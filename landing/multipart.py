"""Minimal multipart/form-data parser. stdlib only; py3.10-3.13+ safe."""
import io

class MultipartParser:
    def __init__(self, headers, stream, max_bytes=52_428_800):
        ctype = headers.get("Content-Type", "")
        if "boundary=" not in ctype:
            raise ValueError("multipart/form-data boundary missing")
        self._boundary = ("--" + ctype.split("boundary=", 1)[1].strip().strip('"')).encode()
        self._max = max_bytes
        total = int(headers.get("Content-Length", "0") or 0)
        if total > max_bytes:
            raise ValueError(f"payload {total} exceeds cap {max_bytes} bytes")
        self._buf = stream.read(max_bytes + 1)
        if len(self._buf) > max_bytes:
            raise ValueError(f"payload exceeds cap {max_bytes} bytes")
        self._parts = [p for p in self._buf.split(self._boundary) if p.strip(b"\r\n-")]
        self._fields = self._parse_fields()

    def _parse_fields(self):
        out = {}
        for part in self._parts:
            if b"\r\n\r\n" not in part: continue
            head, _, body = part.partition(b"\r\n\r\n")
            head_s = head.decode("utf-8", "replace")
            if 'name="' not in head_s: continue
            name = head_s.split('name="', 1)[1].split('"', 1)[0]
            fname = None
            if 'filename="' in head_s:
                fname = head_s.split('filename="', 1)[1].split('"', 1)[0]
            out[name] = (body[:-2] if body.endswith(b"\r\n") else body, fname)
        return out

    def field(self, name, default=""):
        v = self._fields.get(name)
        return v[0].decode("utf-8", "replace") if v else default

    def file_name(self):
        v = self._fields.get("stl")
        return v[1] if v else None

    def file_bytes(self):
        v = self._fields.get("stl")
        if not v: raise ValueError("missing stl file")
        if not v[1]: raise ValueError("stl part has no filename")
        return v[0]
