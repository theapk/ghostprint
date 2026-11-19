import io
from landing.multipart import MultipartParser

def _body(parts, boundary="----gp-b"):
    out = b""
    for name, value, filename in parts:
        cd = f'Content-Disposition: form-data; name="{name}"'
        if filename: cd += f'; filename="{filename}"'
        out += f"--{boundary}\r\n{cd}\r\nContent-Type: application/octet-stream\r\n\r\n".encode() + value + b"\r\n"
    return out + f"--{boundary}--\r\n".encode()

def test_parses_fields_and_file():
    headers = {"Content-Type": "multipart/form-data; boundary=----gp-b"}
    p = MultipartParser(headers, io.BytesIO(_body(
        [("order_id", b"O-1", None), ("printer", b"p", None),
         ("job_seq", b"7", None), ("stl", b"BINSTL", "cube .stl")])))
    assert p.field("order_id") == "O-1" and p.field("job_seq") == "7"
    assert p.file_name() == "cube .stl" and p.file_bytes() == b"BINSTL"

def test_rejects_missing_boundary():
    try:
        MultipartParser({"Content-Type": "application/json"}, io.BytesIO(b""))
        assert False
    except ValueError: pass

def test_caps_size():
    headers = {"Content-Type": "multipart/form-data; boundary=----gp-b"}
    big = _body([("stl", b"x" * 100, "big.stl")])
    try:
        MultipartParser(headers, io.BytesIO(big), max_bytes=50)
        assert False
    except ValueError: pass

def test_binary_stl_with_dashes_survives():
    headers = {"Content-Type": "multipart/form-data; boundary=----gp-b"}
    payload = b"--not-a-real-boundary\r\n" * 40 + b"\x01\x02BINSTL"
    p = MultipartParser(headers, io.BytesIO(_body([("stl", payload, "weird.stl")])))
    assert p.file_bytes() == payload
