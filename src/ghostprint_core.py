"""
ghostprint_core — invisible 3D-print fingerprinting.

Two independent layers, both round-trip verifiable:

  L1. G-code micro-watermark — printer_id + job_id + timestamp encoded as
      sub-resolution Z-babysteps at print-start. Recovered by parsing the
      .gcode file. Survives anything that does not round-trip Z to mm.

  L2. Geometric steganography — order_id hash spread as vertex perturbations
      of magnitude 0.005-0.020 mm (below typical 0.04 mm nozzle X/Y
      resolution). Recovered by diffing the suspect STL against the master.
      IMPORTANT: standard slicers re-mesh, so this layer survives only on
      a no-slicer path or against the original master STL. The encoder
      emits a manifest that pairs master STL with order_id so a verifier
      with access to the master can always recover order_id.

The two layers are independent. L1 alone proves "printer N printed something
at time T" (assuming the .gcode is the artifact under test). L2 alone proves
"this object was the one tagged for order Z" (assuming you have the master).
Together they pin both provenance (which printer) and instance (which order).
"""

from __future__ import annotations

import base64
import hashlib
import struct
import time
import zlib
from dataclasses import asdict, dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

import numpy as np
from stl import mesh as stl_mesh


# --- Wire format -----------------------------------------------------------

# L1 payload layout (12 bytes, then CRC32 in 4 more bytes -> 16 bytes raw,
# base32-encoded into a string prefixed "GP1:"):
#   bytes 0..2  printer_id  (24 bits)
#   bytes 3..5  job_seq     (24 bits, sequence-per-printer)
#   bytes 6..9  unix_ts     (32 bits, full second precision)
#   bytes 10..11 scheme     (16 bits, currently always 0x0001)

L1_SCHEME_VERSION = 1
L1_PREAMBLE = "GP1"

# Layer-2 vertex perturbation envelope. Magnitude in mm.
L2_AMPLITUDE_MIN_MM = 0.005
L2_AMPLITUDE_MAX_MM = 0.020


# --- L1: G-code micro-watermark -------------------------------------------

def l1_encode_payload(printer_id: int, job_seq: int, ts_unix: int) -> bytes:
    """Pack 12 bytes: printer_id(3) | job_seq(3) | ts_unix(4) | scheme(2)."""
    if not (0 <= printer_id < (1 << 24)):
        raise ValueError(f"printer_id out of range: {printer_id}")
    if not (0 <= job_seq < (1 << 24)):
        raise ValueError(f"job_seq out of range: {job_seq}")
    if not (0 <= ts_unix < (1 << 32)):
        raise ValueError(f"ts_unix out of range: {ts_unix}")
    payload = struct.pack(">I", printer_id)[1:]   # 3 bytes, big-endian
    payload += struct.pack(">I", job_seq)[1:]      # 3 bytes
    payload += struct.pack(">I", ts_unix)          # 4 bytes
    payload += struct.pack(">H", L1_SCHEME_VERSION)  # 2 bytes
    assert len(payload) == 12
    return payload


def l1_decode_payload(payload: bytes) -> Tuple[int, int, int]:
    """Inverse of encode. Returns (printer_id, job_seq, ts_unix)."""
    if len(payload) != 12:
        raise ValueError(f"payload must be 12 bytes, got {len(payload)}")
    printer_id = struct.unpack(">I", b"\x00" + payload[0:3])[0]
    job_seq = struct.unpack(">I", b"\x00" + payload[3:6])[0]
    ts_unix = struct.unpack(">I", payload[6:10])[0]
    scheme = struct.unpack(">H", payload[10:12])[0]
    if scheme != L1_SCHEME_VERSION:
        raise ValueError(f"unknown scheme version: {scheme}")
    return printer_id, job_seq, ts_unix


def l1_payload_to_babysteps(payload: bytes, base_z: float = 0.20) -> List[Tuple[float, int]]:
    """
    Map payload bytes + 4 CRC bytes to a sequence of (z_offset_mm, bit)
    moves. Each bit produces a G1 Z move whose sign encodes the bit:
        bit=1 -> +amplitude mm above base_z
        bit=0 -> -amplitude mm below base_z
    Amplitude is 0.010 mm — within a single 0.04 mm layer band, so
    physically invisible on the finished part but recoverable from
    the G-code text.
    """
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    bits: List[int] = []
    for byte in payload:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)
    for i in range(31, -1, -1):
        bits.append((crc >> i) & 1)
    out: List[Tuple[float, int]] = []
    for b in bits:
        z = base_z + (b * 2 - 1) * 0.010
        out.append((z, b))
    return out


def l1_babysteps_to_payload(steps: Sequence[Tuple[float, Optional[int]]],
                            expected_len: int = 12,
                            base_z: float = 0.20) -> bytes:
    """Inverse: recover payload bytes from the (z_offset, bit) tuples.
    `expected_len` is the payload length in bytes (8 default, 12 for v1.1+).
    We read enough steps to cover expected_len*8 + 32 CRC bits."""
    total_data_bits = expected_len * 8
    total_bits = total_data_bits + 32
    if len(steps) < total_bits:
        raise ValueError(f"need >= {total_bits} babysteps, got {len(steps)}")
    bits: List[int] = []
    for z, b in steps[:total_bits]:
        if b is not None and b in (0, 1):
            bits.append(b)
        else:
            # Sign of (z - base_z) tells us the bit.
            bits.append(1 if z > base_z else 0)
    data_bits = bits[:total_data_bits]
    crc_bits = bits[total_data_bits:total_bits]
    payload = bytearray()
    for i in range(0, total_data_bits, 8):
        byte = 0
        for j in range(8):
            byte = (byte << 1) | data_bits[i + j]
        payload.append(byte)
    expected_crc = zlib.crc32(bytes(payload)) & 0xFFFFFFFF
    got_crc = 0
    for i in range(0, 32, 8):
        byte = 0
        for j in range(8):
            byte = (byte << 1) | crc_bits[i + j]
        got_crc = (got_crc << 8) | byte
    if got_crc != expected_crc:
        raise ValueError(f"CRC mismatch: got {got_crc:#010x}, expected {expected_crc:#010x}")
    return bytes(payload)


def l1_rewrite_gcode(gcode: str, printer_id: int, job_seq: int, ts_unix: int,
                     inject_after: str = "; ghostprint-anchor") -> str:
    """
    Splice the L1 watermark block into a G-code string immediately after
    the first line containing `inject_after` (or at the top if missing).
    The block is bracketed by `; --- ghostprint begin/end ---` comments
    and includes a human-readable `; GP1: <base32>` line for redundant
    recovery.
    """
    payload = l1_encode_payload(printer_id, job_seq, ts_unix)
    b32 = base64.b32encode(
        payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)
    ).decode().replace("=", "")

    steps = l1_payload_to_babysteps(payload)
    out_lines: List[str] = []
    out_lines.append("; --- ghostprint begin ---")
    out_lines.append(f"; GP1: {L1_PREAMBLE}:{b32}")
    out_lines.append(f"; printer_id={printer_id} job_seq={job_seq} ts_unix={ts_unix}")
    out_lines.append("; G1 X0 Y0 F6000 ; park for watermark")
    cur_x = 0.0
    for z, bit in steps:
        cur_x += 0.5
        out_lines.append(f"G1 X{cur_x:.3f} Y0 Z{z:.4f} F6000")
    out_lines.append("G1 X0 Y0 Z0.2 F6000 ; park done")
    out_lines.append("; --- ghostprint end ---")
    block = "\n".join(out_lines)

    if inject_after in gcode:
        return gcode.replace(inject_after, inject_after + "\n" + block, 1)
    return f"; {inject_after}\n" + block + "\n" + gcode


def l1_extract_from_gcode(gcode: str) -> dict:
    """
    Recover the L1 watermark from a G-code string.
    Strategy 1: regex the `; GP1: ...` comment.
    Strategy 2: scan the watermark block for Z babysteps and sign-decode.
    """
    import re
    m = re.search(r";\s*GP1:\s*GP1:([A-Z2-7]+)", gcode)
    if m:
        b32 = m.group(1)
        pad = (-len(b32)) % 8
        raw = base64.b32decode(b32 + "=" * pad)
        if len(raw) != 16:
            raise ValueError(f"GP1 base32 wrong length: {len(raw)} (expected 16)")
        payload, crc = raw[:12], struct.unpack(">I", raw[12:16])[0]
        if zlib.crc32(payload) & 0xFFFFFFFF != crc:
            raise ValueError("GP1 CRC mismatch")
        printer_id, job_seq, ts_unix = l1_decode_payload(payload)
        return {
            "printer_id": printer_id,
            "job_seq": job_seq,
            "ts_unix": ts_unix,
            "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_unix)),
            "source": "comment",
        }
    m2 = re.search(
        r";\s*---\s*ghostprint begin\s*---\s*\n(.*?);\s*---\s*ghostprint end\s*---",
        gcode, re.DOTALL,
    )
    if not m2:
        raise ValueError("no GP1 watermark found in gcode")
    block = m2.group(1)
    steps: List[Tuple[float, Optional[int]]] = []
    in_park_done = False
    for line in block.splitlines():
        line = line.strip()
        # Skip the "park done" trailing line (Z0.2 return-to-park) — it's
        # part of the block envelope, not a watermark bit.
        if "park done" in line:
            in_park_done = True
            continue
        if in_park_done:
            continue
        if line.startswith("G1 ") and "Z" in line:
            for tok in line.split():
                if tok.startswith("Z"):
                    try:
                        z = float(tok[1:])
                    except ValueError:
                        continue
                    steps.append((z, None))
                    break
    if not steps:
        raise ValueError("ghostprint block present but no Z moves found")
    payload = l1_babysteps_to_payload(steps, expected_len=12)
    printer_id, job_seq, ts_unix = l1_decode_payload(payload)
    return {
        "printer_id": printer_id,
        "job_seq": job_seq,
        "ts_unix": ts_unix,
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_unix)),
        "source": "babysteps",
    }


# --- L2: geometric steganography ------------------------------------------

def l2_order_hash(order_id: str, master_seed: bytes = b"ghostprint-v1") -> bytes:
    """Domain-separated 32-byte hash of order_id (BLAKE2b)."""
    return hashlib.blake2b(
        master_seed + b"\x00" + order_id.encode("utf-8"),
        digest_size=32,
    ).digest()


def _predict_delta(master_face: np.ndarray, f_idx: int, v_idx: int,
                   h_byte: int, amplitude: float) -> np.ndarray:
    """
    Re-derive the per-vertex perturbation axis (in the encoder's
    deterministic order) so a verifier can predict it. Returns the
    (3,) displacement vector the encoder would have applied.
    """
    n = np.cross(master_face[1] - master_face[0], master_face[2] - master_face[0])
    nn = np.linalg.norm(n)
    if nn < 1e-12:
        return np.zeros(3)
    n /= nn
    ax = int(np.argmax(np.abs(n)))
    axis = np.zeros(3)
    axis[ax] = 1.0
    axis = axis - n * (axis @ n)
    an = np.linalg.norm(axis)
    if an < 1e-6:
        axis = np.array([1.0, 0.0, 0.0])
    else:
        axis /= an
    sign = 1.0 if (h_byte & 0x80) else -1.0
    frac = 0.5 + (h_byte & 0x7F) / 255.0 * 0.5
    return np.round(axis * (sign * amplitude * frac), 4)


def l2_perturb_vertices(master: "stl_mesh.Mesh", order_id: str,
                        amplitude: float = 0.012) -> "stl_mesh.Mesh":
    """
    Return a NEW Mesh whose vertices are perturbed by ±amplitude mm
    (within [L2_AMPLITUDE_MIN_MM, L2_AMPLITUDE_MAX_MM]) along a
    deterministic per-vertex axis driven by the order hash. The axis
    is the world axis with smallest dot to the face normal, then
    projected to be tangent to the face. Shared vertices on triangle
    boundaries move coherently (de-duped by coordinate).
    """
    if amplitude < L2_AMPLITUDE_MIN_MM or amplitude > L2_AMPLITUDE_MAX_MM:
        raise ValueError(
            f"amplitude {amplitude} out of safe range "
            f"[{L2_AMPLITUDE_MIN_MM}, {L2_AMPLITUDE_MAX_MM}] mm"
        )
    h = l2_order_hash(order_id)
    out = stl_mesh.Mesh(np.copy(master.data))
    q = 6  # 1 µm coordinate dedup
    seen: dict = {}  # (x,y,z) rounded -> delta
    # CRITICAL: predict against the UNPERTURBED master, not the live
    # out.vectors view, because we mutate out.vectors in place below and
    # that would distort face normals on subsequent vertices of the same
    # face, breaking the deterministic per-vertex axis.
    for f_idx in range(out.vectors.shape[0]):
        master_face = master.vectors[f_idx]
        for v_idx in range(3):
            key = tuple(np.round(master_face[v_idx], q))
            if key not in seen:
                h_byte = h[(f_idx * 3 + v_idx) % 32]
                seen[key] = _predict_delta(master_face, f_idx, v_idx, h_byte, amplitude)
            out.vectors[f_idx, v_idx] = master_face[v_idx] + seen[key]
    return out


def l2_recover_order(suspect: "stl_mesh.Mesh", master: "stl_mesh.Mesh",
                     amplitude: float = 0.012,
                     candidates: Optional[Iterable[str]] = None) -> Optional[str]:
    """
    Recover order_id by diffing suspect vs master.

    With `candidates`: score each by cosine similarity between observed
    deltas and predicted (axis, sign, fraction) under that candidate's
    hash. The encoder de-dups shared vertices — the FIRST (face, vert)
    occurrence defines the delta; all subsequent occurrences inherit it.
    The verifier reproduces that exact ordering. Returns the best
    candidate if score >= 0.99, else None.

    Without `candidates`: returns None. (For exact match against a
    single candidate, use l2_verify_match.)
    """
    if suspect.vectors.shape != master.vectors.shape:
        raise ValueError(
            f"shape mismatch: suspect {suspect.vectors.shape} vs master {master.vectors.shape}"
        )

    # Build the encoder-faithful (f, v) -> delta mapping for this master.
    q = 6
    primary: dict = {}  # key -> (f_idx, v_idx)
    deltas_by_key: dict = {}
    for f_idx in range(master.vectors.shape[0]):
        for v_idx in range(3):
            key = tuple(np.round(master.vectors[f_idx, v_idx], q))
            d = suspect.vectors[f_idx, v_idx] - master.vectors[f_idx, v_idx]
            if key not in primary:
                primary[key] = (f_idx, v_idx)
                deltas_by_key[key] = d

    if candidates is None:
        return None

    best: Optional[str] = None
    best_score = -1.0
    for cand in candidates:
        h = l2_order_hash(cand)
        score = 0.0
        n = 0
        for key, (pf, pv) in primary.items():
            h_byte = h[(pf * 3 + pv) % 32]
            predicted = _predict_delta(master.vectors[pf], pf, pv, h_byte, amplitude)
            obs = deltas_by_key[key]
            dn = np.linalg.norm(obs)
            if dn < 1e-9:
                score += 0.5
            else:
                pn = np.linalg.norm(predicted)
                if pn < 1e-9:
                    score += 0.0
                else:
                    cos = float(obs @ predicted / (dn * pn))
                    score += max(0.0, cos)
            n += 1
        score /= max(1, n)
        if score > best_score:
            best_score = score
            best = cand
    if best_score < 0.99:
        return None
    return best


def l2_recover_deltas(suspect: "stl_mesh.Mesh", master: "stl_mesh.Mesh") -> np.ndarray:
    """
    Return the (N*3, 3) array of per-vertex deltas (suspect - master),
    flattened in (face, vertex) iteration order. Used by the round-trip
    test to assert exact match against the encoder's prediction.
    """
    if suspect.vectors.shape != master.vectors.shape:
        raise ValueError(
            f"shape mismatch: suspect {suspect.vectors.shape} vs master {master.vectors.shape}"
        )
    return (suspect.vectors - master.vectors).reshape(-1, 3)


def l2_verify_match(suspect: "stl_mesh.Mesh", master: "stl_mesh.Mesh",
                    order_id: str, amplitude: float = 0.012) -> bool:
    """
    Verify that a suspect STL matches a candidate order_id exactly (within
    1e-4 mm — the encoder's rounding precision).

    Mirrors the encoder's de-dup: the first time a vertex coordinate is
    seen, its perturbation is computed from THAT (face, vert) index.
    Subsequent shared vertices inherit the same delta.
    """
    h = l2_order_hash(order_id)
    q = 6
    seen: dict = {}  # (x,y,z) -> delta
    for f_idx in range(master.vectors.shape[0]):
        face = master.vectors[f_idx]
        for v_idx in range(3):
            key = tuple(np.round(face[v_idx], q))
            if key not in seen:
                h_byte = h[(f_idx * 3 + v_idx) % 32]
                seen[key] = _predict_delta(face, f_idx, v_idx, h_byte, amplitude)
            predicted = seen[key]
            observed = suspect.vectors[f_idx, v_idx] - master.vectors[f_idx, v_idx]
            if not np.allclose(observed, predicted, atol=1e-5):
                return False
    return True


# --- Manifest --------------------------------------------------------------

@dataclass
class GhostprintManifest:
    """Sidecar JSON describing what was tagged and how to verify it."""
    order_id: str
    printer_id: int
    job_seq: int
    ts_unix: int
    l1_gcode_watermark: bool
    l2_geom_stego: bool
    l2_amplitude_mm: float
    master_stl_sha256: str
    tagged_stl_sha256: Optional[str]
    scheme_version: int = L1_SCHEME_VERSION

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**d)


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
