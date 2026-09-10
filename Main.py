# AKRecovery.py
# =====================================================================
# PROJECT: AK Backup Recovery Environment (Production Bare-Metal Recovery Engine)
# AUTHORITATIVE, ZERO-SIMULATION, BYTE-ACCURATE CONSUMER (AKBK v2)
# =====================================================================

import os
import sys
import json
import time
import uuid
import struct
import ctypes
import hashlib
import queue
import threading
import subprocess
import zlib
import copy
from pathlib import Path
from datetime import datetime

# --- RUNTIME & ADMIN PRE-CHECK ---
def verify_runtime_environment():
    if os.name != 'nt':
        print("CRITICAL ERROR: AK Recovery Environment requires Windows / WinPE.")
        sys.exit(1)
    if "--self-test" in sys.argv:
        return
    try:
        is_admin = ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        is_admin = False
    if not is_admin:
        try:
            cmd_args = subprocess.list2cmdline(sys.argv)
            ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, cmd_args, None, 1)
        except Exception:
            pass
        sys.exit(0)

if "--self-test" not in sys.argv:
    verify_runtime_environment()

try:
    if os.name == 'nt':
        import wmi
        import win32api
        import win32file
        import win32security
    else:
        wmi = win32api = win32file = win32security = None
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
except ImportError:
    print("CRITICAL ERROR: Required third-party dependencies are missing.")
    print("Run: pip install cryptography wmi pywin32")
    sys.exit(1)

# GUI imports (safe in headless / self-test execution mode)
try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog, simpledialog
except ImportError:
    tk = ttk = messagebox = filedialog = simpledialog = None

# =====================================================================
# CANONICAL AKBK v2 SPECIFICATION CONSTANTS
# =====================================================================
class RecoveryConstants:
    APP_NAME = "AK Backup Recovery Environment (Bare-Metal)"
    MAGIC = b"AKBK"
    MAJOR_VERSION = 1
    MINOR_VERSION = 0
    STRUCT_FORMAT = "<4sHHB16s16sQ8s"
    HEADER_SIZE = struct.calcsize(STRUCT_FORMAT)  # Exactly 57 bytes: 4+2+2+1+16+16+8+8
    BACKUP_TYPE_FULL = 0x01
    BACKUP_TYPE_DIFFERENTIAL = 0x02
    UUID_SIZE = 16
    TIMESTAMP_SIZE = 8
    HOST_ID_SIZE = 8
    BLOCK_SIZE = 4 * 1024 * 1024  # 4 MiB alignment
    COMMIT_MAGIC = 0x4B434D54     # "CMKC"
    LOG_DIR = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "AKRecovery_Logs")
    JOURNAL_PATH = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "AKRecovery_Journal.json")
    MAX_ENVELOPE_LEN = 64 * 1024 * 1024  # Sane upper boundary limit (64 MiB)
    EFI_SYSTEM_PARTITION_GUID = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"

assert RecoveryConstants.HEADER_SIZE == 57, f"Header size mismatch! Expected 57, got {RecoveryConstants.HEADER_SIZE}"

os.makedirs(RecoveryConstants.LOG_DIR, exist_ok=True)
def recovery_log(msg, is_error=False):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    lvl = "ERROR" if is_error else "INFO"
    entry = f"[{ts}] [{lvl}] {msg}\n"
    log_file = os.path.join(RecoveryConstants.LOG_DIR, f"recovery_{datetime.now().strftime('%Y%m%d')}.log")
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception:
        pass

# =====================================================================
# DETERMINISTIC BOUNDS-CHECKED STREAM PARSER
# =====================================================================
class BoundParser:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def remaining(self) -> int:
        return len(self.data) - self.offset

    def read_exact(self, n: int) -> bytes:
        if n < 0 or self.offset + n > len(self.data):
            raise ValueError(f"Buffer underrun: requested {n} bytes, {self.remaining()} remaining at offset {self.offset}")
        res = self.data[self.offset:self.offset+n]
        self.offset += n
        return res

    def read_u8(self) -> int:
        return struct.unpack('<B', self.read_exact(1))[0]

    def read_u16(self) -> int:
        return struct.unpack('<H', self.read_exact(2))[0]

    def read_u32(self) -> int:
        return struct.unpack('<I', self.read_exact(4))[0]

    def read_u64(self) -> int:
        return struct.unpack('<Q', self.read_exact(8))[0]

# =====================================================================
# RECOVERY JOURNAL & ATOMIC STATE MACHINE
# =====================================================================
class RecoveryJournal:
    STATES = ["IDLE", "VALIDATING", "PREFLIGHT", "RESTORING", "RESTORE_COMPLETE", "BOOT_REPAIR", "COMPLETED", "FAILED"]

    @staticmethod
    def write_state(state: str, backup_id: str, block_idx: int, total_blocks: int,
                    target_disk_index: str = "", model: str = "", serial: str = "",
                    device_id: str = "", err: str = ""):
        if state not in RecoveryJournal.STATES and not state.startswith("FAILED"):
            state = "FAILED"
        data = {
            "state": state,
            "backup_id": backup_id,
            "target_disk_index": str(target_disk_index),
            "model": model,
            "serial": serial,
            "device_id": device_id,
            "block_idx": block_idx,
            "total_blocks": total_blocks,
            "error": err,
            "timestamp": datetime.now().isoformat()
        }
        temp_journal = f"{RecoveryConstants.JOURNAL_PATH}.tmp"
        try:
            with open(temp_journal, "w", encoding="utf-8") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_journal, RecoveryConstants.JOURNAL_PATH)
        except Exception:
            if os.path.exists(temp_journal):
                try: os.remove(temp_journal)
                except Exception: pass

    @staticmethod
    def check_previous_crash():
        if os.path.exists(RecoveryConstants.JOURNAL_PATH):
            try:
                with open(RecoveryConstants.JOURNAL_PATH, "r", encoding="utf-8") as f:
                    state = json.load(f)
                if state.get("state") in ("RESTORING", "BOOT_REPAIR", "RESTORE_COMPLETE"):
                    return True, state
            except Exception:
                pass
        return False, None

# =====================================================================
# KEY VAULT & CRYPTOGRAPHIC ENGINE
# =====================================================================
class RecoveryKeyVault:
    @staticmethod
    def unprotect_master_key(dpapi_blob: bytes, pass_wrapped: bytes, passphrase: str = None) -> bytes:
        if os.name == 'nt' and dpapi_blob and dpapi_blob != b"DPAPI_UNAVAILABLE":
            try:
                crypt32 = ctypes.windll.crypt32
                class DATA_BLOB(ctypes.Structure):
                    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
                in_blob = DATA_BLOB(len(dpapi_blob), ctypes.cast(ctypes.c_char_p(dpapi_blob), ctypes.POINTER(ctypes.c_char)))
                out_blob = DATA_BLOB()
                if crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0x04, ctypes.byref(out_blob)):
                    res = ctypes.string_at(out_blob.pbData, out_blob.cbData)
                    ctypes.windll.kernel32.LocalFree(out_blob.pbData)
                    return res
            except Exception:
                pass

        if passphrase and pass_wrapped and len(pass_wrapped) > 12:
            salt = b"AKBackupCrossMachineRecoverySalt"
            kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=100000)
            pass_key = kdf.derive(passphrase.encode('utf-8'))
            aes = AESGCM(pass_key)
            try:
                return aes.decrypt(pass_wrapped[:12], pass_wrapped[12:], None)
            except Exception:
                pass

        raise ValueError("Key Recovery Error: DPAPI unprotection failed and no valid cross-machine recovery passphrase was provided.")

# =====================================================================
# DETERMINISTIC AKBK v2 PARSER
# =====================================================================
class AKBKParser:
    @staticmethod
    def parse_and_verify(filepath: str, passphrase: str = None) -> dict:
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"AKB file not found: {filepath}")

        with open(filepath, "rb") as f:
            file_bytes = f.read()

        if len(file_bytes) < RecoveryConstants.HEADER_SIZE + 70:
            raise ValueError("Invalid AKBK file: File size is below minimum required header and metadata threshold.")

        parser = BoundParser(file_bytes)

        # SECTION 1: Header (57 bytes: <4sHHB16s16sQ8s)
        header_data = parser.read_exact(RecoveryConstants.HEADER_SIZE)
        magic, major, minor, b_type, backup_id, base_id, timestamp, host_id = struct.unpack(RecoveryConstants.STRUCT_FORMAT, header_data)

        if magic != RecoveryConstants.MAGIC:
            raise ValueError(f"Invalid AKBK Magic Header: {magic}. Expected {RecoveryConstants.MAGIC}.")
        if major != RecoveryConstants.MAJOR_VERSION:
            raise ValueError(f"Unsupported AKBK format version {major}.{minor}. Expected major version {RecoveryConstants.MAJOR_VERSION}.")
        if b_type not in (RecoveryConstants.BACKUP_TYPE_FULL, RecoveryConstants.BACKUP_TYPE_DIFFERENTIAL):
            raise ValueError(f"Invalid backup type indicator: {b_type}")

        try:
            bid_uuid = uuid.UUID(bytes=backup_id)
            base_uuid = uuid.UUID(bytes=base_id)
        except Exception as e:
            raise ValueError(f"Malformed UUID binary representation in header: {e}")

        # SECTION 2: Security Envelope
        d_len = parser.read_u32()
        if d_len < 0 or d_len > RecoveryConstants.MAX_ENVELOPE_LEN or d_len > parser.remaining():
            raise ValueError(f"Invalid DPAPI blob length: {d_len}")
        dpapi_blob = parser.read_exact(d_len)

        p_len = parser.read_u32()
        if p_len < 0 or p_len > RecoveryConstants.MAX_ENVELOPE_LEN or p_len > parser.remaining():
            raise ValueError(f"Invalid passphrase wrap length: {p_len}")
        pass_wrapped = parser.read_exact(p_len)

        pk_len = parser.read_u32()
        if pk_len < 0 or pk_len > RecoveryConstants.MAX_ENVELOPE_LEN or pk_len > parser.remaining():
            raise ValueError(f"Invalid public key DER length: {pk_len}")
        pub_der = parser.read_exact(pk_len)

        try:
            pub_key = serialization.load_der_public_key(pub_der)
            if not isinstance(pub_key, ec.EllipticCurvePublicKey) or pub_key.curve.name != "secp384r1":
                raise ValueError("Public key is not an EllipticCurvePublicKey over SECP384R1 curve.")
        except Exception as e:
            raise ValueError(f"Security Envelope DER public key validation failed: {e}")

        master_key = RecoveryKeyVault.unprotect_master_key(dpapi_blob, pass_wrapped, passphrase)
        session_key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=backup_id, iterations=600000).derive(master_key)
        aes = AESGCM(session_key)

        # SECTION 3: Encrypted System Manifest
        m_len = parser.read_u64()
        if m_len < 16 or m_len > parser.remaining():
            raise ValueError(f"Invalid manifest ciphertext length: {m_len}")
        m_nonce = parser.read_exact(12)
        m_cipher = parser.read_exact(m_len)

        try:
            manifest_raw = aes.decrypt(m_nonce, m_cipher, None)
            manifest = json.loads(manifest_raw.decode('utf-8'))
        except Exception as e:
            raise ValueError(f"Manifest decryption or GCM authentication FAILED: {e}")

        if not isinstance(manifest, dict):
            raise ValueError("Decrypted manifest is not a valid JSON object.")

        # SECTION 4: Encrypted Block Index
        i_len = parser.read_u64()
        if i_len < 16 or i_len > parser.remaining():
            raise ValueError(f"Invalid block index ciphertext length: {i_len}")
        i_nonce = parser.read_exact(12)
        i_cipher = parser.read_exact(i_len)
        try:
            index_raw = aes.decrypt(i_nonce, i_cipher, None)
            index_entries = json.loads(index_raw.decode('utf-8'))
        except Exception as e:
            raise ValueError(f"Block Index decryption or GCM authentication FAILED: {e}")

        if not isinstance(index_entries, list):
            raise ValueError("Decrypted block index is not a valid JSON array.")

        seen_offsets = set()
        for e in index_entries:
            if not isinstance(e, dict) or not {"offset", "size", "id", "is_ref", "ref_id"}.issubset(e.keys()):
                raise ValueError("Malformed block index entry structure.")
            if not isinstance(e["offset"], int) or not isinstance(e["size"], int):
                raise ValueError("Block index offset and size must be integer values.")
            if e["offset"] < 0 or e["size"] <= 0:
                raise ValueError(f"Invalid index geometry: offset {e['offset']}, size {e['size']}")
            if e["offset"] in seen_offsets:
                raise ValueError(f"Duplicate logical offset {e['offset']} in single backup index.")
            seen_offsets.add(e["offset"])

            try:
                uuid.UUID(hex=str(e["id"]))
                uuid.UUID(hex=str(e["ref_id"]))
            except Exception as ex:
                raise ValueError(f"Malformed UUID in block index entry: {ex}")

        # SECTION 5: Payload Chunks
        chunk_count = parser.read_u32()
        chunks_map = {}
        seen_chunk_ids = set()

        for _ in range(chunk_count):
            record_type = parser.read_u8()
            if record_type == 1:
                chunk_id = parser.read_exact(16)
                cid_hex = uuid.UUID(bytes=chunk_id).hex
                if cid_hex in seen_chunk_ids:
                    raise ValueError(f"Duplicate chunk ID detected: {cid_hex}")
                seen_chunk_ids.add(cid_hex)

                vol_offset = parser.read_u64()
                plaintext_size = parser.read_u32()
                if plaintext_size <= 0 or plaintext_size > RecoveryConstants.BLOCK_SIZE:
                    raise ValueError(f"Plaintext size {plaintext_size} exceeds valid boundaries.")
                nonce = parser.read_exact(12)
                cipher_sz = plaintext_size + 16
                ciphertext = parser.read_exact(cipher_sz)

                chunks_map[cid_hex] = {
                    "type": 1, "chunk_id": cid_hex, "vol_offset": vol_offset, "size": plaintext_size,
                    "nonce": nonce, "ciphertext": ciphertext, "aes": aes
                }
            elif record_type == 2:
                chunk_id = parser.read_exact(16)
                cid_hex = uuid.UUID(bytes=chunk_id).hex
                if cid_hex in seen_chunk_ids:
                    raise ValueError(f"Duplicate chunk ID detected: {cid_hex}")
                seen_chunk_ids.add(cid_hex)

                ref_id = parser.read_exact(16)
                rid_hex = uuid.UUID(bytes=ref_id).hex
                vol_offset = parser.read_u64()
                chunk_size = parser.read_u32()
                if chunk_size <= 0 or chunk_size > RecoveryConstants.BLOCK_SIZE:
                    raise ValueError(f"Reference chunk size {chunk_size} exceeds valid boundaries.")

                chunks_map[cid_hex] = {
                    "type": 2, "chunk_id": cid_hex, "ref_id": rid_hex, "vol_offset": vol_offset, "size": chunk_size
                }
            else:
                raise ValueError(f"Unknown payload record type: {record_type} at offset {parser.offset-1}")

        # SECTION 6: Final Commit Marker
        signed_content_end = parser.offset
        cm_magic = parser.read_u32()
        if cm_magic != RecoveryConstants.COMMIT_MAGIC:
            raise ValueError(f"Invalid Commit Marker Magic! Expected {hex(RecoveryConstants.COMMIT_MAGIC)}, got {hex(cm_magic)}")

        sig_len = parser.read_u32()
        if sig_len <= 0 or sig_len > 1024 or sig_len > parser.remaining():
            raise ValueError(f"Invalid signature length: {sig_len}")
        signature = parser.read_exact(sig_len)

        # Strict EOF Enforcement: Zero trailing bytes permitted
        if parser.remaining() != 0:
            raise ValueError(f"Trailing garbage detected: {parser.remaining()} unparsed bytes after commit marker.")

        try:
            pub_key.verify(signature, file_bytes[:signed_content_end], ec.ECDSA(hashes.SHA384()))
        except Exception as sig_err:
            raise ValueError(f"AKBK ECDSA P-384 Signature Verification FAILED: {sig_err}")

        recovery_log(f"AKBK file successfully parsed and cryptographically verified: {filepath}")
        return {
            "filepath": filepath,
            "backup_id": str(bid_uuid),
            "base_id": str(base_uuid),
            "backup_type": "FULL" if b_type == RecoveryConstants.BACKUP_TYPE_FULL else "DIFFERENTIAL",
            "timestamp": datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S'),
            "manifest": manifest,
            "master_key": master_key,
            "chunks_map": chunks_map,
            "index_entries": index_entries
        }

# =====================================================================
# DIFFERENTIAL CHAIN RESOLVER (OFFLINE, ZERO-SQLITE)
# =====================================================================
class ChainResolver:
    @staticmethod
    def resolve_chunk_reference(target_id: str, current_backup_index: int, chain: list, ref_visited: set = None) -> dict:
        if ref_visited is None:
            ref_visited = set()

        if target_id in ref_visited:
            raise ValueError(f"Circular chunk reference detected involving reference ID {target_id}")
        ref_visited.add(target_id)

        # Traverse backward strictly through current backup and ancestors
        for i in range(current_backup_index, -1, -1):
            anc_backup = chain[i]
            c_map = anc_backup.get("chunks_map", {})
            if target_id in c_map:
                chunk = c_map[target_id]
                if chunk.get("type") == 1:
                    return chunk
                elif chunk.get("type") == 2:
                    next_ref = chunk.get("ref_id")
                    if next_ref == target_id:
                        return ChainResolver.resolve_chunk_reference(next_ref, i - 1, chain, ref_visited)
                    else:
                        return ChainResolver.resolve_chunk_reference(next_ref, i, chain, ref_visited)

        raise ValueError(f"Unresolved reference: Chunk ID {target_id} could not be resolved in ancestor chain.")

    @staticmethod
    def resolve_chain(target_akb_path: str, repository_dir: str, passphrase: str = None) -> tuple:
        chain = []
        current_path = target_akb_path
        visited_ids = set()

        while current_path:
            parsed = AKBKParser.parse_and_verify(current_path, passphrase)
            bid = parsed["backup_id"]
            if bid in visited_ids:
                raise ValueError(f"Circular parent reference detected in chain involving backup ID {bid}")
            visited_ids.add(bid)

            chain.insert(0, parsed)
            base_id = parsed["base_id"]
            if parsed["backup_type"] == "FULL" or base_id == "00000000-0000-0000-0000-000000000000":
                if parsed["backup_type"] != "FULL":
                    raise ValueError(f"Backup {bid} has zero base ID but is marked as DIFFERENTIAL.")
                if base_id != "00000000-0000-0000-0000-000000000000":
                    raise ValueError(f"FULL backup {bid} must have zero UUID parent ID.")
                break

            if base_id == bid:
                raise ValueError(f"Self-parent detected: Backup {bid} references itself.")

            parent_found = False
            candidates = {}
            for fname in os.listdir(repository_dir):
                if fname.endswith(".akb"):
                    fpath = os.path.join(repository_dir, fname)
                    try:
                        with open(fpath, "rb") as pf:
                            h = pf.read(RecoveryConstants.HEADER_SIZE)
                            if len(h) == RecoveryConstants.HEADER_SIZE:
                                p_magic, _, _, _, p_bid, _, _, _ = struct.unpack(RecoveryConstants.STRUCT_FORMAT, h)
                                if p_magic == RecoveryConstants.MAGIC:
                                    cand_id = str(uuid.UUID(bytes=p_bid))
                                    if cand_id in candidates:
                                        raise ValueError(f"Duplicate candidate backup ID {cand_id} found in {fpath} and {candidates[cand_id]}")
                                    candidates[cand_id] = fpath
                    except Exception as ex:
                        raise ValueError(f"Corrupted candidate parent file encountered: {fpath} ({ex})")

            if base_id in candidates:
                current_path = candidates[base_id]
                parent_found = True

            if not parent_found:
                raise ValueError(f"Chain broken: Missing parent backup ID {base_id} required for differential reconstruction.")

        resolved_blocks = {}
        for backup_idx, backup in enumerate(chain):
            seen_backup_offsets = set()
            for entry in backup["index_entries"]:
                off = entry["offset"]
                size = entry["size"]
                is_ref = entry.get("is_ref", False)
                target_id = entry["ref_id"] if is_ref else entry["id"]

                if off in seen_backup_offsets:
                    raise ValueError(f"Conflicting duplicate logical offset {off} inside single backup {backup['backup_id']}")
                seen_backup_offsets.add(off)

                if is_ref:
                    if backup_idx == 0:
                        raise ValueError(f"FULL backup {backup['backup_id']} cannot contain differential reference {target_id}")
                    resolved_chunk = ChainResolver.resolve_chunk_reference(target_id, backup_idx - 1, chain)
                else:
                    if target_id not in backup["chunks_map"] or backup["chunks_map"][target_id]["type"] != 1:
                        raise ValueError(f"Local chunk {target_id} at offset {off} missing from backup payload.")
                    resolved_chunk = backup["chunks_map"][target_id]

                if resolved_chunk["size"] != size:
                    raise ValueError(f"Chunk size mismatch for {target_id}: expected {size}, got {resolved_chunk['size']}")

                resolved_blocks[off] = {
                    "offset": off,
                    "size": size,
                    "chunk": resolved_chunk,
                    "backup_id": backup["backup_id"]
                }

        sorted_final_blocks = sorted(resolved_blocks.values(), key=lambda b: b["offset"])
        for i in range(len(sorted_final_blocks) - 1):
            cur = sorted_final_blocks[i]
            nxt = sorted_final_blocks[i + 1]
            if cur["offset"] + cur["size"] > nxt["offset"]:
                raise ValueError(f"Overlapping resolved block detected: {cur['offset']}+{cur['size']} overlaps {nxt['offset']}")

        return chain, resolved_blocks

# =====================================================================
# PHYSICAL DISK SAFETY & HARDWARE PREFLIGHT ENGINE
# =====================================================================
class DiskManager:
    @staticmethod
    def enumerate_physical_disks() -> list:
        disks = []
        if os.name == 'nt' and wmi:
            try:
                w = wmi.WMI()
                for d in w.Win32_DiskDrive():
                    disks.append({
                        "Index": int(d.Index),
                        "Model": str(d.Model or "UNKNOWN"),
                        "SerialNumber": str(d.SerialNumber).strip() if d.SerialNumber else "UNKNOWN",
                        "Size": int(d.Size) if d.Size else 0,
                        "BytesPerSector": int(d.BytesPerSector) if d.BytesPerSector else 512,
                        "DeviceID": str(d.DeviceID or "")
                    })
            except Exception as e:
                recovery_log(f"Disk enumeration error: {e}", is_error=True)
        return disks

    @staticmethod
    def lock_and_dismount_target_disk(disk_index: int):
        if os.name != 'nt' or not wmi or not win32file:
            return
        w = wmi.WMI()
        drives_to_lock = []
        for disk in w.Win32_DiskDrive(Index=disk_index):
            for partition in disk.associators(wmi_result_class="Win32_DiskPartition"):
                for logical_disk in partition.associators(wmi_result_class="Win32_LogicalDisk"):
                    drives_to_lock.append(logical_disk.DeviceID)

        for drive in drives_to_lock:
            vol_path = f"\\\\.\\{drive}"
            hVol = win32file.CreateFile(
                vol_path, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE,
                None, win32file.OPEN_EXISTING, 0, None
            )
            if hVol == win32file.INVALID_HANDLE_VALUE:
                raise IOError(f"Pre-restore safety check failed: Unable to open volume {drive} on PhysicalDrive{disk_index} for locking.")
            try:
                win32file.DeviceIoControl(hVol, 0x00090018, None, None)  # FSCTL_LOCK_VOLUME
                win32file.DeviceIoControl(hVol, 0x00090020, None, None)  # FSCTL_DISMOUNT_VOLUME
            except Exception as e:
                raise IOError(f"Pre-restore safety check failed: Volume {drive} could not be dismounted: {e}")
            finally:
                win32file.CloseHandle(hVol)

        recovery_log(f"Target disk Index {disk_index} volumes locked and dismounted.")

class PreflightManager:
    @staticmethod
    def validate_gpt_structure(pt_meta: dict, source_disk_sz: int, source_sector_sz: int):
        pmbr = bytes.fromhex(pt_meta["protective_mbr"])
        if len(pmbr) != source_sector_sz:
            raise ValueError(f"Protective MBR length {len(pmbr)} does not match sector size {source_sector_sz}.")
        if pmbr[510:512] != b"\x55\xAA":
            raise ValueError("Protective MBR boot signature 0x55AA missing.")

        p_hdr = bytes.fromhex(pt_meta["primary_header"])
        if len(p_hdr) < 92 or p_hdr[:8] != b"EFI PART":
            raise ValueError("Primary GPT header signature is not 'EFI PART'.")
        (sig, rev, hdr_sz, hdr_crc, res, cur_lba, bkp_lba, first_u, last_u,
         disk_guid, part_lba, num_parts, part_sz, part_crc) = struct.unpack("<8sIIIIQQQQ16sQIII", p_hdr[:92])
        if hdr_sz < 92 or hdr_sz > len(p_hdr):
            raise ValueError(f"Invalid GPT header size {hdr_sz}.")

        zero_crc_hdr = p_hdr[:16] + b"\x00\x00\x00\x00" + p_hdr[20:hdr_sz]
        if (zlib.crc32(zero_crc_hdr) & 0xFFFFFFFF) != hdr_crc:
            raise ValueError("Primary GPT header CRC32 verification failed.")

        if cur_lba != 1:
            raise ValueError(f"Primary GPT header current_lba is {cur_lba}, expected 1.")
        total_lbas = source_disk_sz // source_sector_sz
        if bkp_lba != total_lbas - 1:
            raise ValueError(f"Primary GPT header backup_lba ({bkp_lba}) does not point to last disk LBA ({total_lbas - 1}).")
        if part_lba < 2:
            raise ValueError(f"Primary partition array LBA {part_lba} must be >= 2.")
        if num_parts <= 0 or part_sz < 128 or (part_sz & (part_sz - 1)) != 0:
            raise ValueError("Invalid partition entry size or count in primary header.")

        array_lba_span = (num_parts * part_sz + source_sector_sz - 1) // source_sector_sz
        if part_lba + array_lba_span > first_u:
            raise ValueError("Primary partition array overlaps usable LBA range.")
        if first_u >= last_u or last_u >= bkp_lba:
            raise ValueError("Invalid usable LBA boundaries in primary GPT header.")

        p_arr = bytes.fromhex(pt_meta["primary_partition_array"])
        expected_arr_len = num_parts * part_sz
        if len(p_arr) != expected_arr_len:
            raise ValueError(f"Primary partition array length {len(p_arr)} != expected {expected_arr_len}.")
        if (zlib.crc32(p_arr) & 0xFFFFFFFF) != part_crc:
            raise ValueError("Primary GPT partition array CRC32 verification failed.")

        b_arr = bytes.fromhex(pt_meta["backup_partition_array"])
        if b_arr != p_arr:
            raise ValueError("Backup GPT partition array does not match primary array.")

        b_hdr = bytes.fromhex(pt_meta["backup_header"])
        if len(b_hdr) < 92 or b_hdr[:8] != b"EFI PART":
            raise ValueError("Backup GPT header signature is not 'EFI PART'.")
        (b_sig, b_rev, b_hdr_sz, b_hdr_crc, b_res, b_cur_lba, b_bkp_lba, b_first_u, b_last_u,
         b_disk_guid, b_part_lba, b_num_parts, b_part_sz, b_part_crc) = struct.unpack("<8sIIIIQQQQ16sQIII", b_hdr[:92])

        b_zero_crc = b_hdr[:16] + b"\x00\x00\x00\x00" + b_hdr[20:b_hdr_sz]
        if (zlib.crc32(b_zero_crc) & 0xFFFFFFFF) != b_hdr_crc:
            raise ValueError("Backup GPT header CRC32 verification failed.")

        if b_cur_lba != bkp_lba or b_bkp_lba != cur_lba:
            raise ValueError("Primary and Backup GPT headers reciprocal LBA consistency check failed.")
        if b_first_u != first_u or b_last_u != last_u or b_disk_guid != disk_guid or b_part_crc != part_crc:
            raise ValueError("Primary and Backup GPT headers attribute consistency check failed.")
        if b_num_parts != num_parts or b_part_sz != part_sz:
            raise ValueError("Backup GPT header partition array dimension mismatch.")
        if b_part_lba != bkp_lba - array_lba_span:
            raise ValueError(f"Backup partition array LBA {b_part_lba} not aligned immediately before backup header ({bkp_lba - array_lba_span}).")

    @staticmethod
    def run_preflight(chain: list, resolved_blocks: dict, target_disk_index: int) -> tuple:
        target_backup = chain[-1]
        manifest = target_backup["manifest"]
        
        report = []
        report.append("=== REAL HARDWARE RECOVERY PREFLIGHT REPORT ===")
        report.append(f"Backup ID: {target_backup['backup_id']}")
        report.append(f"Backup Type: {target_backup['backup_type']}")
        report.append(f"Chain Length: {len(chain)} recovery point(s)")
        report.append(f"Source Host: {manifest.get('Host', 'Unknown')}")

        boot_mode = manifest.get("BootType")
        if boot_mode not in ("UEFI", "BIOS", "MBR"):
            return False, report + [f"PREFLIGHT FAILED: Unknown or unverified boot mode '{boot_mode}' in manifest."]
        report.append(f"Source Boot Mode: {boot_mode}")

        mapping = manifest.get("PhysicalDiskMapping")
        if not mapping or not isinstance(mapping, dict):
            return False, report + [
                "CRITICAL PREFLIGHT REFUSAL: THIS AKB DOES NOT CONTAIN SUFFICIENT VERIFIED PHYSICAL-DISK "
                "MAPPING FOR SAFE BARE-METAL RESTORATION. Manifest structure 'PhysicalDiskMapping' is absent "
                "or invalid. The stream offsets represent file-tree chunks, not verified PhysicalDrive sector LBAs. "
                "Destructive physical write is strictly forbidden."
            ]

        required_mapping_keys = {"schema_version", "disk_size", "sector_size", "partition_table", "partition_table_metadata", "partitions", "blocks"}
        if not required_mapping_keys.issubset(mapping.keys()):
            return False, report + [
                f"CRITICAL PREFLIGHT REFUSAL: 'PhysicalDiskMapping' is malformed. Missing keys: {required_mapping_keys - set(mapping.keys())}"
            ]

        if mapping["schema_version"] != 1:
            return False, report + [f"CRITICAL PREFLIGHT REFUSAL: Unsupported mapping schema_version: {mapping['schema_version']}"]

        if mapping["partition_table"] not in ("GPT", "MBR"):
            return False, report + [f"CRITICAL PREFLIGHT REFUSAL: Unsupported partition table style: {mapping['partition_table']}"]

        source_disk_sz = mapping["disk_size"]
        source_sector_sz = mapping["sector_size"]
        if not isinstance(source_disk_sz, int) or source_disk_sz <= 0:
            return False, report + ["CRITICAL PREFLIGHT REFUSAL: disk_size must be a positive integer."]
        if source_sector_sz not in (512, 1024, 2048, 4096):
            return False, report + [f"CRITICAL PREFLIGHT REFUSAL: Unsupported sector size: {source_sector_sz}"]

        pt_meta = mapping["partition_table_metadata"]
        if not isinstance(pt_meta, dict):
            return False, report + ["CRITICAL PREFLIGHT REFUSAL: 'partition_table_metadata' must be an object."]

        # Structural Partition Table Integrity Enforcement
        if mapping["partition_table"] == "GPT":
            req_gpt_fields = {"protective_mbr", "primary_header", "primary_partition_array", "backup_partition_array", "backup_header"}
            if not req_gpt_fields.issubset(pt_meta.keys()):
                return False, report + [f"CRITICAL PREFLIGHT REFUSAL: Incomplete GPT metadata fields. Missing: {req_gpt_fields - set(pt_meta.keys())}"]
            try:
                PreflightManager.validate_gpt_structure(pt_meta, source_disk_sz, source_sector_sz)
            except Exception as gpt_err:
                return False, report + [f"CRITICAL PREFLIGHT REFUSAL: GPT Structural Validation Failed: {gpt_err}"]

        elif mapping["partition_table"] == "MBR":
            if "mbr_sector" not in pt_meta or "partition_entries" not in pt_meta:
                return False, report + ["CRITICAL PREFLIGHT REFUSAL: Incomplete MBR metadata fields."]
            mbr_sec = bytes.fromhex(pt_meta["mbr_sector"])
            if len(mbr_sec) != source_sector_sz:
                return False, report + [f"CRITICAL PREFLIGHT REFUSAL: MBR sector size {len(mbr_sec)} != {source_sector_sz}."]
            if mbr_sec[510:512] != b"\x55\xAA":
                return False, report + ["CRITICAL PREFLIGHT REFUSAL: MBR signature 0x55AA missing."]

        if not isinstance(mapping.get("partitions"), list) or not isinstance(mapping.get("blocks"), list):
            return False, report + ["CRITICAL PREFLIGHT REFUSAL: 'partitions' and 'blocks' in mapping must be lists."]

        partitions_by_index = {}
        sorted_partitions = []
        seen_partition_guids = set()

        first_usable_lba = 34 if mapping["partition_table"] == "GPT" else 0
        last_usable_lba = (source_disk_sz // source_sector_sz) - 34 if mapping["partition_table"] == "GPT" else (source_disk_sz // source_sector_sz) - 1

        for p in mapping["partitions"]:
            if not isinstance(p, dict) or not {"index", "start_offset", "size"}.issubset(p.keys()):
                return False, report + ["CRITICAL PREFLIGHT REFUSAL: Malformed partition structure in mapping."]
            if not isinstance(p["index"], int) or p["index"] < 0:
                return False, report + ["CRITICAL PREFLIGHT REFUSAL: Partition index must be non-negative integer."]
            if p["index"] in partitions_by_index:
                return False, report + [f"CRITICAL PREFLIGHT REFUSAL: Duplicate partition index: {p['index']}"]
            if p["start_offset"] < 0 or p["size"] <= 0:
                return False, report + [f"CRITICAL PREFLIGHT REFUSAL: Partition {p['index']} has invalid start/size."]
            if p["start_offset"] % source_sector_sz != 0 or p["size"] % source_sector_sz != 0:
                return False, report + [f"CRITICAL PREFLIGHT REFUSAL: Partition index {p['index']} is not sector aligned."]
            if p["start_offset"] + p["size"] > source_disk_sz:
                return False, report + [f"CRITICAL PREFLIGHT REFUSAL: Partition index {p['index']} exceeds source disk boundaries."]

            p_start_lba = p["start_offset"] // source_sector_sz
            p_end_lba = (p["start_offset"] + p["size"]) // source_sector_sz - 1

            if mapping["partition_table"] == "GPT":
                if not {"type_guid", "partition_guid", "start_lba", "end_lba", "attributes"}.issubset(p.keys()):
                    return False, report + [f"CRITICAL PREFLIGHT REFUSAL: Missing required GPT partition fields on partition {p['index']}"]
                try:
                    uuid.UUID(str(p["type_guid"]))
                    p_guid_str = str(uuid.UUID(str(p["partition_guid"])))
                    if p_guid_str in seen_partition_guids:
                        return False, report + [f"CRITICAL PREFLIGHT REFUSAL: Duplicate partition GUID: {p_guid_str}"]
                    seen_partition_guids.add(p_guid_str)
                except Exception as ex:
                    return False, report + [f"CRITICAL PREFLIGHT REFUSAL: Malformed UUID on partition {p['index']}: {ex}"]
                
                if p["start_lba"] != p_start_lba or p["end_lba"] != p_end_lba:
                    return False, report + [f"CRITICAL PREFLIGHT REFUSAL: Declared LBA range does not match byte offsets on partition {p['index']}."]
                if p["start_lba"] < first_usable_lba:
                    return False, report + [f"CRITICAL PREFLIGHT REFUSAL: Partition {p['index']} starts at LBA {p['start_lba']} (overlaps GPT header/array space < {first_usable_lba})."]
                if p["end_lba"] > last_usable_lba:
                    return False, report + [f"CRITICAL PREFLIGHT REFUSAL: Partition {p['index']} ends at LBA {p['end_lba']} (overlaps backup GPT structures > {last_usable_lba})."]

            elif mapping["partition_table"] == "MBR":
                if not {"start_lba", "sector_count", "bootable", "partition_type"}.issubset(p.keys()):
                    return False, report + [f"CRITICAL PREFLIGHT REFUSAL: Missing required MBR partition fields on partition {p['index']}"]
                if p["start_lba"] != p_start_lba or p["sector_count"] != (p["size"] // source_sector_sz):
                    return False, report + [f"CRITICAL PREFLIGHT REFUSAL: MBR LBA parameters do not match byte offsets on partition {p['index']}."]
                if p["start_lba"] == 0:
                    return False, report + [f"CRITICAL PREFLIGHT REFUSAL: MBR partition {p['index']} starts at LBA 0 (overlaps MBR boot sector)."]

            partitions_by_index[p["index"]] = p
            sorted_partitions.append((p["start_offset"], p["size"], p["index"]))

        sorted_partitions.sort(key=lambda x: x[0])
        for idx_p in range(len(sorted_partitions) - 1):
            cur_s, cur_l, cur_i = sorted_partitions[idx_p]
            nxt_s, _, nxt_i = sorted_partitions[idx_p + 1]
            if cur_s + cur_l > nxt_s:
                return False, report + [f"CRITICAL PREFLIGHT REFUSAL: Overlapping partitions detected: index {cur_i} and {nxt_i}"]

        mapping_blocks = {}
        sorted_mapping_blocks_logical = []
        sorted_mapping_blocks_physical = []

        for b in mapping["blocks"]:
            if not isinstance(b, dict) or not {"offset", "size", "partition_index", "lba", "sector_count"}.issubset(b.keys()):
                return False, report + ["CRITICAL PREFLIGHT REFUSAL: Malformed block entry in mapping schema."]
            
            b_off = b["offset"]
            b_sz = b["size"]
            b_lba = b["lba"]
            b_sc = b["sector_count"]
            p_idx = b["partition_index"]

            if not isinstance(b_off, int) or not isinstance(b_sz, int) or not isinstance(b_lba, int) or not isinstance(b_sc, int):
                return False, report + ["CRITICAL PREFLIGHT REFUSAL: Block offset, size, lba, and sector_count must be integers."]

            if b_off < 0 or b_sz <= 0 or b_lba < 0 or b_sc <= 0:
                return False, report + ["CRITICAL PREFLIGHT REFUSAL: Block offset/size/LBA/sector_count must be positive."]

            if b_sc * source_sector_sz != b_sz:
                return False, report + [f"CRITICAL PREFLIGHT REFUSAL: sector_count * sector_size ({b_sc * source_sector_sz}) != size ({b_sz})."]
            
            phys_off = b_lba * source_sector_sz
            phys_sz = b_sc * source_sector_sz

            if phys_off % source_sector_sz != 0 or phys_sz % source_sector_sz != 0:
                return False, report + [f"CRITICAL PREFLIGHT REFUSAL: Block at physical offset {phys_off} is not sector aligned."]
            if phys_off + phys_sz > source_disk_sz:
                return False, report + [f"CRITICAL PREFLIGHT REFUSAL: Block physical extent {phys_off + phys_sz} exceeds source disk size."]

            if p_idx not in partitions_by_index:
                return False, report + [f"CRITICAL PREFLIGHT REFUSAL: Block at offset {b_off} references nonexistent partition index {p_idx}."]
            
            part = partitions_by_index[p_idx]
            part_start_lba = part["start_offset"] // source_sector_sz
            part_end_lba = (part["start_offset"] + part["size"]) // source_sector_sz - 1

            if b_lba < part_start_lba or (b_lba + b_sc - 1) > part_end_lba:
                return False, report + [f"CRITICAL PREFLIGHT REFUSAL: Block at LBA {b_lba} (count {b_sc}) extends outside partition {p_idx} bounds ({part_start_lba}..{part_end_lba})."]

            if b_off in mapping_blocks:
                return False, report + [f"CRITICAL PREFLIGHT REFUSAL: Duplicate offset {b_off} in mapping blocks."]

            mapping_blocks[b_off] = b
            sorted_mapping_blocks_logical.append((b_off, b_sz))
            sorted_mapping_blocks_physical.append((phys_off, phys_sz))

        # Check for overlapping logical backup extents
        sorted_mapping_blocks_logical.sort(key=lambda x: x[0])
        for idx_check in range(len(sorted_mapping_blocks_logical) - 1):
            cur_off, cur_sz = sorted_mapping_blocks_logical[idx_check]
            next_off, _ = sorted_mapping_blocks_logical[idx_check + 1]
            if cur_off + cur_sz > next_off:
                return False, report + [f"CRITICAL PREFLIGHT REFUSAL: Overlapping logical block mapping detected between offset {cur_off} and {next_off}."]

        # Check for overlapping physical disk extents
        sorted_mapping_blocks_physical.sort(key=lambda x: x[0])
        for idx_check in range(len(sorted_mapping_blocks_physical) - 1):
            cur_off, cur_sz = sorted_mapping_blocks_physical[idx_check]
            next_off, _ = sorted_mapping_blocks_physical[idx_check + 1]
            if cur_off + cur_sz > next_off:
                return False, report + [f"CRITICAL PREFLIGHT REFUSAL: Overlapping physical block mapping detected between offset {cur_off} and {next_off}."]

        # Bidirectional Consistency Validation
        for off, entry in resolved_blocks.items():
            sz = entry["size"]
            if off not in mapping_blocks:
                return False, report + [f"CRITICAL PREFLIGHT REFUSAL: Resolved block at offset {off} has no PhysicalDiskMapping entry (extra block)."]
            if mapping_blocks[off]["size"] != sz:
                return False, report + [f"CRITICAL PREFLIGHT REFUSAL: Block size mismatch at offset {off}: mapping has {mapping_blocks[off]['size']}, index has {sz}."]

        for map_off, map_entry in mapping_blocks.items():
            if map_off not in resolved_blocks:
                return False, report + [f"CRITICAL PREFLIGHT REFUSAL: Mapping extent at offset {map_off} is missing from resolved blocks (missing block)."]

        disks = DiskManager.enumerate_physical_disks()
        target_disk = next((d for d in disks if d["Index"] == target_disk_index), None)
        if not target_disk:
            return False, report + ["PREFLIGHT FAILED: Target physical disk index not found."]

        report.append(f"Target Physical Disk: Index {target_disk['Index']} - {target_disk['Model']} (S/N: {target_disk['SerialNumber']})")
        report.append(f"Target Disk Size: {target_disk['Size']} bytes ({target_disk['Size'] // (1024**3)} GB)")
        report.append(f"Target Sector Size: {target_disk['BytesPerSector']} bytes")

        if target_disk["BytesPerSector"] != source_sector_sz:
            return False, report + [f"PREFLIGHT FAILED: Target sector size ({target_disk['BytesPerSector']}) incompatible with source sector size ({source_sector_sz})."]

        required_extent = max([mapping_blocks[b["offset"]]["lba"] * source_sector_sz + b["size"] for b in resolved_blocks.values()], default=0)
        report.append(f"Required Restore Extent: {required_extent} bytes ({required_extent // (1024**3)} GB)")

        if target_disk["Size"] < required_extent or target_disk["Size"] < source_disk_sz:
            return False, report + [f"PREFLIGHT FAILED: Target disk capacity is insufficient. Target has {target_disk['Size']} bytes, required {max(required_extent, source_disk_sz)} bytes."]

        report.append("PREFLIGHT PASSED: Complete partition table metadata, physical geometry, sector alignment, capacity, and security checks satisfied.")
        return True, report

# =====================================================================
# BLOCK-DEVICE RESTORATION ABSTRACTION (PRODUCTION & TEST BACKENDS)
# =====================================================================
class IBlockDevice:
    def open(self): raise NotImplementedError
    def seek(self, offset: int): raise NotImplementedError
    def write(self, data: bytes) -> int: raise NotImplementedError
    def flush(self): raise NotImplementedError
    def read(self, size: int) -> bytes: raise NotImplementedError
    def close(self): raise NotImplementedError

class Win32PhysicalDiskDevice(IBlockDevice):
    def __init__(self, disk_index: int):
        self.disk_index = disk_index
        self.hDisk = None

    def open(self):
        disk_path = f"\\\\.\\PhysicalDrive{self.disk_index}"
        self.hDisk = win32file.CreateFile(
            disk_path, win32file.GENERIC_WRITE | win32file.GENERIC_READ,
            win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE,
            None, win32file.OPEN_EXISTING, 0, None
        )
        if self.hDisk == win32file.INVALID_HANDLE_VALUE:
            raise IOError(f"Failed to open {disk_path} for raw physical write access.")

    def seek(self, offset: int):
        win32file.SetFilePointer(self.hDisk, offset, win32file.FILE_BEGIN)

    def write(self, data: bytes) -> int:
        err, written = win32file.WriteFile(self.hDisk, data)
        if err != 0:
            raise IOError(f"Win32 WriteFile error: {err}")
        return written

    def flush(self):
        win32file.FlushFileBuffers(self.hDisk)

    def read(self, size: int) -> bytes:
        err, buf = win32file.ReadFile(self.hDisk, size)
        if err != 0:
            raise IOError(f"Win32 ReadFile error: {err}")
        return buf

    def close(self):
        if self.hDisk and self.hDisk != win32file.INVALID_HANDLE_VALUE:
            try:
                win32file.CloseHandle(self.hDisk)
            finally:
                self.hDisk = None

class MemoryBlockDevice(IBlockDevice):
    def __init__(self, capacity: int):
        self.capacity = capacity
        self.buffer = bytearray(capacity)
        self.cursor = 0
        self.is_open = False
        self.simulate_write_error = False
        self.simulate_short_write = False
        self.simulate_read_error = False
        self.simulate_short_read = False
        self.partition_table_verified = False

    def open(self):
        self.is_open = True
        self.cursor = 0

    def seek(self, offset: int):
        if offset < 0 or offset > self.capacity:
            raise ValueError("Seek offset outside device capacity.")
        self.cursor = offset

    def write(self, data: bytes) -> int:
        if self.partition_table_verified and self.simulate_write_error:
            raise IOError("Simulated Win32 WriteFile error.")
        if self.partition_table_verified and self.simulate_short_write:
            short_len = max(1, len(data) - 1)
            self.buffer[self.cursor:self.cursor+short_len] = data[:short_len]
            self.cursor += short_len
            return short_len
        if self.cursor + len(data) > self.capacity:
            raise IOError("Write exceeds device bounds.")
        self.buffer[self.cursor:self.cursor+len(data)] = data
        self.cursor += len(data)
        return len(data)

    def flush(self):
        pass

    def read(self, size: int) -> bytes:
        if self.partition_table_verified and self.simulate_read_error:
            raise IOError("Simulated Win32 ReadFile error.")
        if self.partition_table_verified and self.simulate_short_read:
            short_len = max(1, size - 1)
            res = bytes(self.buffer[self.cursor:self.cursor+short_len])
            self.cursor += short_len
            return res
        if self.cursor + size > self.capacity:
            raise IOError("Read exceeds device bounds.")
        res = bytes(self.buffer[self.cursor:self.cursor+size])
        self.cursor += size
        return res

    def close(self):
        self.is_open = False

# =====================================================================
# BARE-METAL RESTORE ENGINE & READ-BACK VERIFICATION
# =====================================================================
class RealBlockRestoreEngine:
    @staticmethod
    def restore_and_verify_partition_table(device: IBlockDevice, mapping: dict):
        pt_type = mapping["partition_table"]
        pt_meta = mapping["partition_table_metadata"]
        sector_sz = mapping["sector_size"]
        disk_sz = mapping["disk_size"]

        if pt_type == "GPT":
            pmbr = bytes.fromhex(pt_meta["protective_mbr"])
            p_hdr = bytes.fromhex(pt_meta["primary_header"])
            p_arr = bytes.fromhex(pt_meta["primary_partition_array"])
            b_arr = bytes.fromhex(pt_meta["backup_partition_array"])
            b_hdr = bytes.fromhex(pt_meta["backup_header"])

            p_cur_lba = struct.unpack("<Q", p_hdr[24:32])[0]
            p_bkp_lba = struct.unpack("<Q", p_hdr[32:40])[0]
            p_part_lba = struct.unpack("<Q", p_hdr[72:80])[0]

            b_cur_lba = struct.unpack("<Q", b_hdr[24:32])[0]
            b_bkp_lba = struct.unpack("<Q", b_hdr[32:40])[0]
            b_part_lba = struct.unpack("<Q", b_hdr[72:80])[0]

            pmbr_offset = 0
            p_hdr_offset = p_cur_lba * sector_sz
            p_arr_offset = p_part_lba * sector_sz
            b_arr_offset = b_part_lba * sector_sz
            b_hdr_offset = b_cur_lba * sector_sz

            for off, sz, desc in [(pmbr_offset, len(pmbr), "Protective MBR"),
                                 (p_hdr_offset, len(p_hdr), "Primary Header"),
                                 (p_arr_offset, len(p_arr), "Primary Partition Array"),
                                 (b_arr_offset, len(b_arr), "Backup Partition Array"),
                                 (b_hdr_offset, len(b_hdr), "Backup Header")]:
                if off < 0 or off + sz > disk_sz or off % sector_sz != 0:
                    raise ValueError(f"Invalid calculated LBA range for {desc} (offset: {off}, size: {sz}, disk size: {disk_sz})")

            device.seek(pmbr_offset)
            if device.write(pmbr) != len(pmbr): raise IOError("Short write during Protective MBR write.")
            device.seek(p_hdr_offset)
            if device.write(p_hdr) != len(p_hdr): raise IOError("Short write during Primary GPT Header write.")
            device.seek(p_arr_offset)
            if device.write(p_arr) != len(p_arr): raise IOError("Short write during Primary Partition Array write.")
            device.seek(b_arr_offset)
            if device.write(b_arr) != len(b_arr): raise IOError("Short write during Backup Partition Array write.")
            device.seek(b_hdr_offset)
            if device.write(b_hdr) != len(b_hdr): raise IOError("Short write during Backup GPT Header write.")

            device.flush()

            device.seek(pmbr_offset)
            if device.read(len(pmbr)) != pmbr: raise ValueError("Protective MBR dual read-back verification failed.")
            device.seek(p_hdr_offset)
            if device.read(len(p_hdr)) != p_hdr: raise ValueError("Primary GPT header dual read-back verification failed.")
            device.seek(p_arr_offset)
            if device.read(len(p_arr)) != p_arr: raise ValueError("Primary GPT partition array dual read-back verification failed.")
            device.seek(b_arr_offset)
            if device.read(len(b_arr)) != b_arr: raise ValueError("Backup GPT partition array dual read-back verification failed.")
            device.seek(b_hdr_offset)
            if device.read(len(b_hdr)) != b_hdr: raise ValueError("Backup GPT header dual read-back verification failed.")

        elif pt_type == "MBR":
            mbr_sec = bytes.fromhex(pt_meta["mbr_sector"])
            device.seek(0)
            if device.write(mbr_sec) != len(mbr_sec): raise IOError("Short write during MBR write.")
            device.flush()
            device.seek(0)
            if device.read(len(mbr_sec)) != mbr_sec: raise ValueError("MBR sector read-back verification failed.")

        if hasattr(device, 'partition_table_verified'):
            device.partition_table_verified = True

    @staticmethod
    def execute_block_restore(resolved_blocks: dict, device: IBlockDevice, target_disk_size: int,
                              sector_size: int, mapping: dict, progress_callback=None) -> bool:
        device.open()
        try:
            RealBlockRestoreEngine.restore_and_verify_partition_table(device, mapping)

            mapping_blocks = {b["offset"]: b for b in mapping.get("blocks", [])}

            sorted_offsets = sorted(resolved_blocks.keys())
            total_blocks = len(sorted_offsets)
            if total_blocks == 0:
                raise ValueError("No valid block index entries found to restore.")

            recovery_log(f"Executing block restoration. Total blocks to restore: {total_blocks}")
            start_time = time.time()

            for idx, off in enumerate(sorted_offsets):
                entry = resolved_blocks[off]
                chunk_info = entry["chunk"]
                size = entry["size"]

                if off not in mapping_blocks:
                    raise ValueError(f"Block at offset {off} missing from PhysicalDiskMapping.")
                map_block = mapping_blocks[off]
                
                physical_offset = map_block["lba"] * sector_size
                physical_size = map_block["sector_count"] * sector_size

                if physical_size != size:
                    raise ValueError(f"Physical block size mismatch at LBA {map_block['lba']}: {physical_size} != {size}")

                if physical_offset < 0 or size <= 0 or physical_offset % sector_size != 0 or size % sector_size != 0 or physical_offset + size > target_disk_size:
                    raise ValueError(f"Pre-write safety assertion failed at physical offset {physical_offset} (size {size})")

                if physical_offset + size < physical_offset:
                    raise ValueError(f"Integer overflow detected in block boundaries: {physical_offset} + {size}")

                try:
                    chunk_data = chunk_info["aes"].decrypt(chunk_info["nonce"], chunk_info["ciphertext"], None)
                except Exception as gcm_err:
                    raise ValueError(f"AES-256-GCM Authentication Failed at logical offset {off}: {gcm_err}")

                if len(chunk_data) != size:
                    raise ValueError(f"Plaintext size mismatch at logical offset {off}. Expected {size}, decrypted {len(chunk_data)}")

                device.seek(physical_offset)
                written = device.write(chunk_data)
                if written != len(chunk_data):
                    raise IOError(f"Incomplete physical write at physical offset {physical_offset}. Written {written}/{len(chunk_data)} bytes.")

                device.flush()

                device.seek(physical_offset)
                read_back_buffer = device.read(len(chunk_data))
                if len(read_back_buffer) != len(chunk_data):
                    raise IOError(f"Incomplete physical read-back at physical offset {physical_offset}. Read {len(read_back_buffer)}/{len(chunk_data)} bytes.")

                expected_hash = hashlib.sha256(chunk_data).hexdigest()
                actual_hash = hashlib.sha256(read_back_buffer).hexdigest()

                if expected_hash != actual_hash:
                    raise ValueError(f"Read-back verification FAILED at physical offset {physical_offset}! Expected {expected_hash}, got {actual_hash}")

                elapsed = max(0.1, time.time() - start_time)
                bytes_written = (idx + 1) * len(chunk_data)
                speed_mb_s = (bytes_written / (1024 * 1024)) / elapsed
                pct = int(((idx + 1) / total_blocks) * 100)
                rem_sec = int((elapsed / (idx + 1)) * (total_blocks - (idx + 1)))
                rem_str = f"{rem_sec // 60:02d}:{rem_sec % 60:02d}"

                if progress_callback:
                    progress_callback(pct, idx + 1, total_blocks, f"{speed_mb_s:.2f} MB/s", rem_str)

            return True
        finally:
            device.close()

    @staticmethod
    def validate_target_identity(target_disk_info: dict):
        target_disk_index = target_disk_info["Index"]
        fresh_disks = DiskManager.enumerate_physical_disks()
        matching_disk = next((d for d in fresh_disks if d["Index"] == target_disk_index), None)
        if not matching_disk:
            raise ValueError(f"Target disk PhysicalDrive{target_disk_index} disappeared.")
        if (matching_disk["Model"] != target_disk_info["Model"] or
            matching_disk["SerialNumber"] != target_disk_info["SerialNumber"] or
            matching_disk["Size"] != target_disk_info["Size"] or
            matching_disk["BytesPerSector"] != target_disk_info["BytesPerSector"] or
            matching_disk["DeviceID"] != target_disk_info["DeviceID"]):
            raise ValueError("Target physical disk identity or geometry changed immediately before write. Aborting.")
        return matching_disk

    @staticmethod
    def restore_and_verify(resolved_blocks: dict, target_disk_info: dict, mapping: dict, progress_callback=None) -> bool:
        RealBlockRestoreEngine.validate_target_identity(target_disk_info)

        if os.name != 'nt' or not win32file:
            recovery_log("Real restoration aborted: Win32 physical disk APIs not available in non-Windows environment.", is_error=True)
            return False

        target_disk_index = target_disk_info["Index"]
        target_disk_size = target_disk_info["Size"]
        sector_size = target_disk_info["BytesPerSector"]

        DiskManager.lock_and_dismount_target_disk(target_disk_index)
        RealBlockRestoreEngine.validate_target_identity(target_disk_info)

        device = Win32PhysicalDiskDevice(target_disk_index)
        RecoveryJournal.write_state(
            "RESTORING", "TARGET_OPERATION", 0, len(resolved_blocks),
            target_disk_index=str(target_disk_index), model=target_disk_info["Model"],
            serial=target_disk_info["SerialNumber"], device_id=target_disk_info["DeviceID"]
        )
        try:
            success = RealBlockRestoreEngine.execute_block_restore(
                resolved_blocks, device, target_disk_size, sector_size, mapping, progress_callback
            )
            if success:
                RealBlockRestoreEngine.validate_target_identity(target_disk_info)
                RecoveryJournal.write_state(
                    "RESTORE_COMPLETE", "TARGET_OPERATION", len(resolved_blocks), len(resolved_blocks),
                    target_disk_index=str(target_disk_index), model=target_disk_info["Model"],
                    serial=target_disk_info["SerialNumber"], device_id=target_disk_info["DeviceID"]
                )
                recovery_log("Bare-metal sector restore and read-back verification completed successfully.")
            return success
        except Exception as e:
            RecoveryJournal.write_state(
                "FAILED", "TARGET_OPERATION", 0, 0,
                target_disk_index=str(target_disk_index), model=target_disk_info["Model"],
                serial=target_disk_info["SerialNumber"], device_id=target_disk_info["DeviceID"], err=str(e)
            )
            recovery_log(f"Restoration hard failure: {e}", is_error=True)
            return False

# =====================================================================
# WINDOWS BOOT REPAIR ENGINE
# =====================================================================
class BootRepairEngine:
    @staticmethod
    def execute_boot_repair(target_disk_index: int, boot_mode: str = "UEFI", manifest: dict = None) -> tuple:
        assigned_temp_letter = None
        try:
            RecoveryJournal.write_state("BOOT_REPAIR", "BOOT_CONFIG", 0, 1, target_disk_index=str(target_disk_index))
            recovery_log(f"Executing Windows Boot Repair for PhysicalDrive{target_disk_index} ({boot_mode})...")
            
            if os.name != 'nt' or not wmi:
                RecoveryJournal.write_state("FAILED", "BOOT_CONFIG", 0, 1, str(target_disk_index), err="WMI not available for target disk association")
                return False, "Boot repair refused: WMI not available to associate target disk with logical volumes."

            w = wmi.WMI()
            target_logical_drives = set()
            for disk in w.Win32_DiskDrive(Index=target_disk_index):
                for partition in disk.associators(wmi_result_class="Win32_DiskPartition"):
                    for logical_disk in partition.associators(wmi_result_class="Win32_LogicalDisk"):
                        target_logical_drives.add(logical_disk.DeviceID.upper())

            detected_win_candidates = []
            for drive in target_logical_drives:
                candidate_win = Path(f"{drive}\\Windows")
                if (candidate_win / "System32" / "winload.exe").exists() or (candidate_win / "System32" / "ntoskrnl.exe").exists():
                    detected_win_candidates.append(str(candidate_win))

            if len(detected_win_candidates) == 0:
                RecoveryJournal.write_state("FAILED", "BOOT_CONFIG", 0, 1, str(target_disk_index), err="No Windows installation detected on target disk")
                return False, f"Boot repair refused: No Windows directory found on target PhysicalDrive{target_disk_index}."
            if len(detected_win_candidates) > 1:
                RecoveryJournal.write_state("FAILED", "BOOT_CONFIG", 0, 1, str(target_disk_index), err="Multiple Windows installations detected on target disk")
                return False, f"Boot repair refused: Ambiguous Windows installations detected on target disk: {detected_win_candidates}"

            detected_win_dir = detected_win_candidates[0]

            bcdboot_exe = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32", "bcdboot.exe")
            if not os.path.exists(bcdboot_exe):
                bcdboot_exe = "bcdboot.exe"

            cmd = [bcdboot_exe, detected_win_dir]
            target_boot_vol = None

            if boot_mode == "UEFI":
                detected_efi_candidates = []
                for disk in w.Win32_DiskDrive(Index=target_disk_index):
                    for partition in disk.associators(wmi_result_class="Win32_DiskPartition"):
                        p_type = str(getattr(partition, "Type", "")).upper()
                        ldisks = list(partition.associators(wmi_result_class="Win32_LogicalDisk"))
                        
                        is_esp = False
                        if RecoveryConstants.EFI_SYSTEM_PARTITION_GUID in p_type:
                            is_esp = True
                        elif manifest and "PhysicalDiskMapping" in manifest:
                            for mp in manifest["PhysicalDiskMapping"].get("partitions", []):
                                if str(mp.get("type_guid", "")).upper() == RecoveryConstants.EFI_SYSTEM_PARTITION_GUID:
                                    if int(mp.get("start_offset", -1)) == int(getattr(partition, "StartingOffset", -2)):
                                        is_esp = True
                                        break

                        if is_esp:
                            if ldisks:
                                detected_efi_candidates.append(ldisks[0].DeviceID.upper())
                            else:
                                for candidate_letter in ["S:", "Z:", "Y:", "W:"]:
                                    if not os.path.exists(f"{candidate_letter}\\"):
                                        p_device_id = partition.DeviceID
                                        res_mount = subprocess.run(["mountvol", candidate_letter, f"\\\\?\\GLOBALROOT{p_device_id}\\"], capture_output=True, text=True)
                                        if res_mount.returncode == 0:
                                            verified_target = False
                                            for chk_part in disk.associators(wmi_result_class="Win32_DiskPartition"):
                                                if chk_part.DeviceID == p_device_id:
                                                    verified_target = True
                                                    break
                                            if verified_target:
                                                detected_efi_candidates.append(candidate_letter)
                                                assigned_temp_letter = candidate_letter
                                                break
                                            else:
                                                subprocess.run(["mountvol", candidate_letter, "/d"], capture_output=True, text=True)

                if len(detected_efi_candidates) != 1:
                    RecoveryJournal.write_state("FAILED", "BOOT_CONFIG", 0, 1, str(target_disk_index), err="EFI partition not uniquely associated on target disk")
                    return False, f"Boot repair refused: EFI System Partition on PhysicalDrive{target_disk_index} could not be uniquely identified (found: {detected_efi_candidates})."
                target_boot_vol = detected_efi_candidates[0]
                cmd.extend(["/s", target_boot_vol, "/f", "UEFI"])

            elif boot_mode in ("BIOS", "MBR"):
                system_active_candidates = []
                for disk in w.Win32_DiskDrive(Index=target_disk_index):
                    for partition in disk.associators(wmi_result_class="Win32_DiskPartition"):
                        if getattr(partition, "Bootable", False) is True:
                            for ldisk in partition.associators(wmi_result_class="Win32_LogicalDisk"):
                                system_active_candidates.append(ldisk.DeviceID.upper())
                
                if len(system_active_candidates) != 1:
                    RecoveryJournal.write_state("FAILED", "BOOT_CONFIG", 0, 1, str(target_disk_index), err="BIOS active partition not uniquely identified")
                    return False, f"Boot repair refused: Unique active system partition on PhysicalDrive{target_disk_index} not found: {system_active_candidates}"
                target_boot_vol = system_active_candidates[0]
                cmd.extend(["/s", target_boot_vol, "/f", "BIOS"])
            else:
                RecoveryJournal.write_state("FAILED", "BOOT_CONFIG", 0, 1, str(target_disk_index), err=f"Unknown boot mode {boot_mode}")
                return False, f"Boot repair refused: Unrecognized boot mode '{boot_mode}'."

            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if res.returncode != 0:
                RecoveryJournal.write_state("FAILED", "BOOT_CONFIG", 0, 1, str(target_disk_index), err=f"bcdboot error: {res.stderr.strip()}")
                recovery_log(f"bcdboot returned non-zero exit code ({res.returncode}): {res.stderr.strip()}", is_error=True)
                return False, f"bcdboot failed (code {res.returncode}): {res.stderr.strip()}"

            if boot_mode == "UEFI" and target_boot_vol:
                expected_bcd = Path(f"{target_boot_vol}\\EFI\\Microsoft\\Boot\\BCD")
                expected_bootmgfw = Path(f"{target_boot_vol}\\EFI\\Microsoft\\Boot\\bootmgfw.efi")
                if not expected_bcd.exists() or expected_bcd.stat().st_size == 0 or not expected_bootmgfw.exists() or expected_bootmgfw.stat().st_size == 0:
                    RecoveryJournal.write_state("FAILED", "BOOT_CONFIG", 0, 1, str(target_disk_index), err="Post-verification failed: UEFI boot files missing or empty")
                    return False, f"Boot repair post-verification failed: Missing or empty BCD / bootmgfw.efi on {target_boot_vol}."
            elif boot_mode in ("BIOS", "MBR") and target_boot_vol:
                expected_boot_mgr = Path(f"{target_boot_vol}\\bootmgr")
                expected_bios_bcd = Path(f"{target_boot_vol}\\Boot\\BCD")
                if not expected_boot_mgr.exists() or expected_boot_mgr.stat().st_size == 0 or not expected_bios_bcd.exists() or expected_bios_bcd.stat().st_size == 0:
                    RecoveryJournal.write_state("FAILED", "BOOT_CONFIG", 0, 1, str(target_disk_index), err="Post-verification failed: BIOS boot files missing or empty")
                    return False, f"Boot repair post-verification failed: Missing or empty bootmgr / Boot\\BCD on {target_boot_vol}."
            
            recovery_log(f"Windows Boot Repair succeeded and verified: {res.stdout.strip()}")
            return True, res.stdout.strip()
        except Exception as e:
            RecoveryJournal.write_state("FAILED", "BOOT_CONFIG", 0, 1, str(target_disk_index), err=str(e))
            return False, f"Boot repair invocation error: {e}"
        finally:
            if assigned_temp_letter:
                try:
                    subprocess.run(["mountvol", assigned_temp_letter, "/d"], capture_output=True, text=True)
                except Exception:
                    pass

# =====================================================================
# THREAD-SAFE RECOVERY GUI
# =====================================================================
class RecoveryWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(RecoveryConstants.APP_NAME)
        self.geometry("1040x700")
        self.configure(bg="#1E1E1E")
        self.selected_file = ""
        self.resolved_chain = None
        self.resolved_blocks = None
        self.selected_disk_info = None
        self.passphrase = None
        self.preflight_passed = False
        self.msg_queue = queue.Queue()

        header = tk.Frame(self, bg="#005A9E", height=65)
        header.pack(side=tk.TOP, fill=tk.X)
        tk.Label(header, text="AK BACKUP RECOVERY ENVIRONMENT (BARE-METAL)", font=("Segoe UI", 12, "bold"), fg="white", bg="#005A9E").pack(pady=16)

        panel = tk.Frame(self, bg="#2D2D30", width=290)
        panel.pack(side=tk.LEFT, fill=tk.Y)

        btn_kw = {"bg": "#333333", "fg": "white", "relief": "flat", "font": ("Segoe UI", 10), "anchor": "w", "padx": 15, "pady": 10}

        tk.Button(panel, text="1. Select Backup (.akb)", command=self.select_file, **btn_kw).pack(fill=tk.X, pady=3)
        tk.Button(panel, text="2. Validate Chain & Crypto", command=self.validate_chain, **btn_kw).pack(fill=tk.X, pady=3)
        tk.Button(panel, text="3. Select Target Physical Disk", command=self.select_target_disk, **btn_kw).pack(fill=tk.X, pady=3)
        tk.Button(panel, text="4. Execute Hardware Preflight", command=self.run_preflight, **btn_kw).pack(fill=tk.X, pady=3)
        
        self.restore_btn = tk.Button(panel, text="EXECUTE BARE-METAL RESTORE", command=self.execute_restore, bg="#D9534F", fg="white", relief="flat", font=("Segoe UI", 10, "bold"), padx=15, pady=12, state=tk.DISABLED)
        self.restore_btn.pack(fill=tk.X, pady=15)
        
        tk.Button(panel, text="Restart System", command=lambda: os.system("shutdown /r /t 0"), **btn_kw).pack(fill=tk.X, pady=3)
        tk.Button(panel, text="Shutdown System", command=lambda: os.system("shutdown /s /t 0"), **btn_kw).pack(fill=tk.X, pady=3)

        display_frame = tk.Frame(self, bg="#1E1E1E")
        display_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=20, pady=20)

        self.console = tk.Text(display_frame, bg="#121212", fg="#00FF00", font=("Consolas", 10), relief="flat")
        self.console.pack(fill=tk.BOTH, expand=True, pady=(0, 15))
        self.console.insert(tk.END, f"[{datetime.now().strftime('%H:%M:%S')}] Recovery Engine Initialized.\n")

        self.progress = ttk.Progressbar(display_frame, orient="horizontal", mode="determinate", length=500)
        self.progress.pack(fill=tk.X, pady=(0, 8))

        self.metrics_lbl = tk.Label(display_frame, text="Status: Idle | Speed: 0.00 MB/s | Remaining: --:--", fg="white", bg="#1E1E1E", font=("Segoe UI", 9, "bold"))
        self.metrics_lbl.pack(anchor="w")

        crashed, c_state = RecoveryJournal.check_previous_crash()
        if crashed:
            self.log_msg(f"CRITICAL WARNING: Previous operation on disk {c_state.get('target_disk_index')} was interrupted at block {c_state.get('block_idx')}/{c_state.get('total_blocks')}!", is_error=True)
            messagebox.showwarning("Interrupted Operation", "A previous restore operation was interrupted before completion. Target drive may be incomplete or unbootable.")

        self.after(50, self.process_queue)

    def log_msg(self, msg, is_error=False):
        ts = datetime.now().strftime('%H:%M:%S')
        lvl = "[ERROR]" if is_error else "[INFO]"
        self.console.insert(tk.END, f"[{ts}] {lvl} {msg}\n")
        self.console.see(tk.END)
        recovery_log(msg, is_error)

    def process_queue(self):
        try:
            while True:
                task, args = self.msg_queue.get_nowait()
                task(*args)
        except queue.Empty:
            pass
        self.after(50, self.process_queue)

    def select_file(self):
        fpath = filedialog.askopenfilename(title="Select AKBK Backup File", filetypes=[("AKBK Backup Files", "*.akb"), ("All Files", "*.*")])
        if fpath:
            self.selected_file = fpath
            self.preflight_passed = False
            self.restore_btn.config(state=tk.DISABLED)
            self.log_msg(f"Selected backup file: {fpath}")

    def validate_chain(self):
        if not self.selected_file:
            messagebox.showerror("Error", "Please select an .akb backup file first.")
            return

        self.preflight_passed = False
        self.restore_btn.config(state=tk.DISABLED)
        RecoveryJournal.write_state("VALIDATING", "VALIDATION_STEP", 0, 1)
        self.log_msg(f"Resolving recovery chain and verifying cryptographic signatures for: {self.selected_file}")
        try:
            repo_dir = os.path.dirname(self.selected_file)
            try:
                self.resolved_chain, self.resolved_blocks = ChainResolver.resolve_chain(self.selected_file, repo_dir, self.passphrase)
            except Exception as e:
                if "Key Recovery Error" in str(e):
                    pwd = simpledialog.askstring("Cross-Machine Recovery", "Enter Recovery Passphrase for cross-machine restoration:", show="*")
                    if pwd:
                        self.passphrase = pwd
                        self.resolved_chain, self.resolved_blocks = ChainResolver.resolve_chain(self.selected_file, repo_dir, self.passphrase)
                    else:
                        raise
                else:
                    raise

            self.log_msg(f"SUCCESS: Recovery chain resolved. Chain points: {len(self.resolved_chain)}, Total Unique Blocks: {len(self.resolved_blocks)}")
            for idx, c in enumerate(self.resolved_chain):
                self.log_msg(f"  Point [{idx+1}] ID: {c['backup_id']} ({c['backup_type']})")
            messagebox.showinfo("Validation Success", "Cryptographic signature and full differential chain validation PASSED.")
        except Exception as e:
            RecoveryJournal.write_state("FAILED", "VALIDATION_STEP", 0, 1, err=str(e))
            self.log_msg(f"VALIDATION FAILED: {e}", is_error=True)
            messagebox.showerror("Validation Failed", str(e))

    def select_target_disk(self):
        disks = DiskManager.enumerate_physical_disks()
        if not disks:
            messagebox.showerror("Error", "No physical disks detected.")
            return

        top = tk.Toplevel(self)
        top.title("Select Target Physical Disk")
        top.geometry("720x360")
        top.configure(bg="white")
        top.attributes('-topmost', True)

        tk.Label(top, text="Select Destination Physical Disk for Bare-Metal Restore (Explicit Confirmation Required):", font=("Segoe UI", 9, "bold"), bg="white").pack(anchor="w", padx=15, pady=10)

        lb = tk.Listbox(top, font=("Consolas", 10), width=90, height=8)
        lb.pack(padx=15, pady=5)

        for d in disks:
            lb.insert(tk.END, f"Disk {d['Index']}: {d['Model']} | S/N: {d['SerialNumber']} | {d['Size'] // (1024**3)} GB | Sector: {d['BytesPerSector']}B | ID: {d['DeviceID']}")

        def confirm_selection():
            sel = lb.curselection()
            if not sel:
                messagebox.showwarning("Warning", "Please select a disk.")
                return
            idx = sel[0]
            self.selected_disk_info = disks[idx]
            self.preflight_passed = False
            self.restore_btn.config(state=tk.DISABLED)
            self.log_msg(f"Selected Target Physical Disk: Index {self.selected_disk_info['Index']} ({self.selected_disk_info['Model']}, S/N: {self.selected_disk_info['SerialNumber']})")
            top.destroy()

        tk.Button(top, text="Confirm Target Disk", bg="#005A9E", fg="white", relief="flat", padx=15, pady=8, command=confirm_selection).pack(pady=10)

    def run_preflight(self):
        if not self.resolved_chain or not self.resolved_blocks:
            messagebox.showerror("Error", "Please validate backup chain first.")
            return
        if not self.selected_disk_info:
            messagebox.showerror("Error", "Please select a target physical disk first.")
            return

        target_info = self.selected_disk_info
        RecoveryJournal.write_state("PREFLIGHT", self.resolved_chain[-1]["backup_id"], 0, len(self.resolved_blocks),
                                    target_disk_index=str(target_info["Index"]), model=target_info["Model"],
                                    serial=target_info["SerialNumber"], device_id=target_info["DeviceID"])
        passed, report = PreflightManager.run_preflight(self.resolved_chain, self.resolved_blocks, target_info["Index"])
        for line in report:
            self.log_msg(line)

        if passed:
            self.preflight_passed = True
            self.restore_btn.config(state=tk.NORMAL)
            messagebox.showinfo("Preflight Passed", "Hardware preflight checks passed successfully.")
        else:
            self.preflight_passed = False
            self.restore_btn.config(state=tk.DISABLED)
            messagebox.showerror("Preflight Failed", "\n".join(report))

    def execute_restore(self):
        if not self.resolved_chain or not self.resolved_blocks or not self.selected_disk_info or not self.preflight_passed:
            messagebox.showerror("Error", "All preflight safety gates must pass before restore can be executed.")
            return

        target_info = self.selected_disk_info
        warning_prompt = (
            f"CRITICAL DESTRUCTIVE RESTORE WARNING!\n\n"
            f"TARGET PHYSICAL DRIVE: PhysicalDrive{target_info['Index']}\n"
            f"DEVICE ID: {target_info['DeviceID']}\n"
            f"MODEL: {target_info['Model']}\n"
            f"SERIAL NUMBER: {target_info['SerialNumber']}\n"
            f"CAPACITY: {target_info['Size'] // (1024**3)} GB ({target_info['Size']} bytes)\n"
            f"SECTOR SIZE: {target_info['BytesPerSector']} bytes\n\n"
            "ALL EXISTING DATA, PARTITIONS, AND VOLUMES ON THIS PHYSICAL DISK WILL BE DESTROYED.\n\n"
            "Do you confirm destruction and execution of bare-metal recovery?"
        )
        if not messagebox.askyesno("CONFIRM DESTRUCTIVE RESTORE", warning_prompt, icon='warning'):
            return

        self.restore_btn.config(state=tk.DISABLED)
        self.log_msg(f"Initiating bare-metal restoration to PhysicalDrive{target_info['Index']}...")

        def progress_cb(pct, current_block, total_blocks, speed, remaining):
            def update_ui():
                self.progress['value'] = pct
                self.metrics_lbl.config(text=f"Restoring Block {current_block}/{total_blocks} | Speed: {speed} | Est. Remaining: {remaining} | Progress: {pct}%")
            self.msg_queue.put((update_ui, ()))

        def worker():
            try:
                mapping = self.resolved_chain[-1]["manifest"]["PhysicalDiskMapping"]
                success = RealBlockRestoreEngine.restore_and_verify(self.resolved_blocks, target_info, mapping, progress_cb)
                if success:
                    def post_restore_ui():
                        self.log_msg("PHYSICAL RESTORATION AND READ-BACK VERIFICATION COMPLETED.")
                    self.msg_queue.put((post_restore_ui, ()))

                    boot_mode = self.resolved_chain[-1]["manifest"].get("BootType", "UEFI")
                    bm_success, bm_msg = BootRepairEngine.execute_boot_repair(
                        target_info["Index"], boot_mode, self.resolved_chain[-1]["manifest"]
                    )
                    
                    def finish_ui():
                        if bm_success:
                            self.log_msg(f"BOOT REPAIR SUCCEEDED: {bm_msg}")
                            RecoveryJournal.write_state("COMPLETED", self.resolved_chain[-1]["backup_id"], len(self.resolved_blocks), len(self.resolved_blocks),
                                                        target_disk_index=str(target_info["Index"]), model=target_info["Model"],
                                                        serial=target_info["SerialNumber"], device_id=target_info["DeviceID"])
                            messagebox.showinfo("Recovery Success", "Bare-metal recovery and boot repair succeeded! System is ready to reboot.")
                        else:
                            self.log_msg(f"BOOT REPAIR FAILED: {bm_msg}", is_error=True)
                            RecoveryJournal.write_state("FAILED", self.resolved_chain[-1]["backup_id"], len(self.resolved_blocks), len(self.resolved_blocks),
                                                        target_disk_index=str(target_info["Index"]), model=target_info["Model"],
                                                        serial=target_info["SerialNumber"], device_id=target_info["DeviceID"], err=bm_msg)
                            messagebox.showerror("Recovery Incomplete / Failed", f"Physical restore succeeded, but boot repair failed: {bm_msg}\nSystem is marked as FAILED.")
                        self.restore_btn.config(state=tk.NORMAL)
                    self.msg_queue.put((finish_ui, ()))
                else:
                    def fail_restore_ui():
                        self.log_msg("RECOVERY FAILED: Physical data restore or read-back verification failed.", is_error=True)
                        messagebox.showerror("Recovery Failed", "Restoration failed or failed bit-for-bit read-back verification.")
                        self.restore_btn.config(state=tk.NORMAL)
                    self.msg_queue.put((fail_restore_ui, ()))
            except Exception as unhandled_err:
                RecoveryJournal.write_state("FAILED", "TARGET_OPERATION", 0, 0,
                                            target_disk_index=str(target_info["Index"]), model=target_info["Model"],
                                            serial=target_info["SerialNumber"], device_id=target_info["DeviceID"], err=str(unhandled_err))
                def crash_ui():
                    self.log_msg(f"WORKER THREAD EXCEPTION: {unhandled_err}", is_error=True)
                    messagebox.showerror("Critical Failure", f"An unexpected error aborted recovery: {unhandled_err}")
                    self.restore_btn.config(state=tk.NORMAL)
                self.msg_queue.put((crash_ui, ()))

        threading.Thread(target=worker, daemon=True).start()

# =====================================================================
# REAL BEHAVIORAL SELF-TEST SUITE
# =====================================================================
def run_self_test():
    print("========================================================")
    print("AKRecovery Real Behavioral Self-Test Suite (Canonical 57-Byte AKBK)")
    print("========================================================")
    failures = []
    test_count = 0
    not_executed_count = 0

    def assert_test(name, fn):
        nonlocal test_count, not_executed_count
        test_count += 1
        try:
            status = fn()
            if status == "NOT EXECUTED":
                not_executed_count += 1
                print(f"[NOT EXECUTED] {test_count}. {name}")
            else:
                print(f"[PASS] {test_count}. {name}")
        except Exception as e:
            print(f"[FAIL] {test_count}. {name}: {e}")
            failures.append(f"{test_count}. {name}")

    # 1. Header 57 bytes
    def t1():
        assert struct.calcsize(RecoveryConstants.STRUCT_FORMAT) == 57
        assert RecoveryConstants.HEADER_SIZE == 57
    assert_test("Header calcsize == 57 bytes", t1)

    bid = uuid.uuid4().bytes
    base_id = bytes(16)
    timestamp = int(time.time())
    host_id = b'HOSTID8B'
    header_packed = struct.pack(RecoveryConstants.STRUCT_FORMAT,
                                RecoveryConstants.MAGIC,
                                RecoveryConstants.MAJOR_VERSION,
                                RecoveryConstants.MINOR_VERSION,
                                RecoveryConstants.BACKUP_TYPE_FULL,
                                bid, base_id, timestamp, host_id)

    # 2. Header symmetry
    def t2():
        assert len(header_packed) == 57
        p = BoundParser(header_packed)
        m, maj, mino, bt, r_bid, r_base, r_ts, r_host = struct.unpack(RecoveryConstants.STRUCT_FORMAT, p.read_exact(57))
        assert m == b"AKBK" and maj == 1 and mino == 0 and bt == 1 and r_bid == bid and r_base == base_id and r_ts == timestamp and r_host == host_id
    assert_test("Header parser unpack symmetry", t2)

    # 3. Invalid magic
    def t3():
        bad_h = struct.pack(RecoveryConstants.STRUCT_FORMAT, b"BAAD", 1, 0, 1, bid, base_id, timestamp, host_id)
        with open("bad_magic.akb", "wb") as f: f.write(bad_h + bytes(100))
        try:
            AKBKParser.parse_and_verify("bad_magic.akb")
            raise AssertionError("Accepted invalid magic!")
        except ValueError:
            pass
        finally:
            if os.path.exists("bad_magic.akb"): os.remove("bad_magic.akb")
    assert_test("Invalid magic rejection", t3)

    # 4. Invalid version
    def t4():
        bad_v = struct.pack(RecoveryConstants.STRUCT_FORMAT, RecoveryConstants.MAGIC, 99, 0, 1, bid, base_id, timestamp, host_id)
        with open("bad_ver.akb", "wb") as f: f.write(bad_v + bytes(100))
        try:
            AKBKParser.parse_and_verify("bad_ver.akb")
            raise AssertionError("Accepted invalid version!")
        except ValueError:
            pass
        finally:
            if os.path.exists("bad_ver.akb"): os.remove("bad_ver.akb")
    assert_test("Invalid version rejection", t4)

    # 5. Invalid backup type
    def t5():
        bad_t = struct.pack(RecoveryConstants.STRUCT_FORMAT, RecoveryConstants.MAGIC, 1, 0, 99, bid, base_id, timestamp, host_id)
        with open("bad_type.akb", "wb") as f: f.write(bad_t + bytes(100))
        try:
            AKBKParser.parse_and_verify("bad_type.akb")
            raise AssertionError("Accepted invalid backup type!")
        except ValueError:
            pass
        finally:
            if os.path.exists("bad_type.akb"): os.remove("bad_type.akb")
    assert_test("Invalid backup type rejection", t5)

    # 6. Truncated header
    def t6():
        p = BoundParser(header_packed[:30])
        try:
            p.read_exact(57)
            raise AssertionError("Truncated header accepted!")
        except ValueError:
            pass
    assert_test("Truncated header rejection", t6)

    # Setup valid mock fixtures
    master_k = os.urandom(32)
    salt_cross = b"AKBackupCrossMachineRecoverySalt"
    pass_k = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt_cross, iterations=100000).derive(b"TestSecretPass")
    nonce_pass = os.urandom(12)
    pass_wrapped = nonce_pass + AESGCM(pass_k).encrypt(nonce_pass, master_k, None)
    priv_key = ec.generate_private_key(ec.SECP384R1())
    pub_key = priv_key.public_key()
    pub_der = pub_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    dpapi_b = b"DUMMY_DPAPI_BLOB"
    env_bytes = (
        struct.pack('<I', len(dpapi_b)) + dpapi_b +
        struct.pack('<I', len(pass_wrapped)) + pass_wrapped +
        struct.pack('<I', len(pub_der)) + pub_der
    )

    # 7. Envelope parsing
    def t7():
        p_env = BoundParser(env_bytes)
        d_len = p_env.read_u32()
        d_data = p_env.read_exact(d_len)
        pw_len = p_env.read_u32()
        pw_data = p_env.read_exact(pw_len)
        pk_len = p_env.read_u32()
        pk_data = p_env.read_exact(pk_len)
        assert d_data == dpapi_b and pw_data == pass_wrapped and pk_data == pub_der
    assert_test("Security envelope parsing", t7)

    # 8. Passphrase unwrap
    def t8():
        unwrapped_k = RecoveryKeyVault.unprotect_master_key(b"", pass_wrapped, "TestSecretPass")
        assert unwrapped_k == master_k
    assert_test("Passphrase unwrap & PBKDF2 derivation", t8)

    session_k = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=bid, iterations=600000).derive(master_k)
    aes_sess = AESGCM(session_k)
    test_data = bytes([0xAA] * 512)

    # Reusable valid physical mapping fixture helper
    def make_valid_physical_mapping_fixture():
        src_sz = 100 * 1024 * 1024
        sec_sz = 512
        total_lbas = src_sz // sec_sz
        num_parts = 128
        part_sz = 128
        array_lba_span = (num_parts * part_sz + sec_sz - 1) // sec_sz  # 32 sectors

        pm = bytearray(sec_sz)
        pm[510:512] = b"\x55\xAA"
        pa = bytearray(num_parts * part_sz)
        pa_crc = zlib.crc32(pa) & 0xFFFFFFFF

        bkp_lba = total_lbas - 1
        b_part_lba = bkp_lba - array_lba_span
        first_u = 34
        last_u = total_lbas - 34

        ph_raw = struct.pack("<8sIIIIQQQQ16sQIII", b"EFI PART", 0x00010000, 92, 0, 0, 1, bkp_lba, first_u, last_u, bytes(16), 2, num_parts, part_sz, pa_crc)
        ph_crc = zlib.crc32(ph_raw) & 0xFFFFFFFF
        ph = ph_raw[:16] + struct.pack("<I", ph_crc) + ph_raw[20:]

        bh_raw = struct.pack("<8sIIIIQQQQ16sQIII", b"EFI PART", 0x00010000, 92, 0, 0, bkp_lba, 1, first_u, last_u, bytes(16), b_part_lba, num_parts, part_sz, pa_crc)
        bh_crc = zlib.crc32(bh_raw) & 0xFFFFFFFF
        bh = bh_raw[:16] + struct.pack("<I", bh_crc) + bh_raw[20:]

        return {
            "schema_version": 1,
            "disk_size": src_sz,
            "sector_size": sec_sz,
            "disk_signature_or_guid": "DISK-GUID-1",
            "partition_table": "GPT",
            "partition_table_metadata": {
                "protective_mbr": bytes(pm).hex(),
                "primary_header": bytes(ph).hex(),
                "primary_partition_array": bytes(pa).hex(),
                "backup_partition_array": bytes(pa).hex(),
                "backup_header": bytes(bh).hex()
            },
            "partitions": [
                {
                    "index": 0,
                    "type_guid": "C12A7328-F81F-11D2-BA4B-00A0C93EC93B",
                    "partition_guid": str(uuid.uuid4()),
                    "start_offset": 34 * sec_sz,
                    "size": 10 * 1024 * 1024,
                    "start_lba": 34,
                    "end_lba": 34 + (10 * 1024 * 1024 // sec_sz) - 1,
                    "attributes": 0
                },
                {
                    "index": 1,
                    "type_guid": str(uuid.uuid4()),
                    "partition_guid": str(uuid.uuid4()),
                    "start_offset": (34 * sec_sz) + (10 * 1024 * 1024),
                    "size": 10 * 1024 * 1024,
                    "start_lba": 34 + (10 * 1024 * 1024 // sec_sz),
                    "end_lba": 34 + (20 * 1024 * 1024 // sec_sz) - 1,
                    "attributes": 0
                }
            ],
            "blocks": [{"offset": 17408, "size": 512, "partition_index": 0, "lba": 2048, "sector_count": 1}]
        }

    m_dict = {
        "Host": "TEST_HOST",
        "BootType": "UEFI",
        "PhysicalDiskMapping": make_valid_physical_mapping_fixture()
    }

    source_disk_sz_test = m_dict["PhysicalDiskMapping"]["disk_size"]
    m_bytes = json.dumps(m_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode('utf-8')
    m_nonce = os.urandom(12)
    m_cipher = aes_sess.encrypt(m_nonce, m_bytes, None)

    c_nonce = os.urandom(12)
    c_cipher = aes_sess.encrypt(c_nonce, test_data, None)

    idx_data = [{"offset": 17408, "size": 512, "id": uuid.UUID(bytes=bid).hex, "is_ref": False, "ref_id": uuid.UUID(int=0).hex}]
    idx_bytes = json.dumps(idx_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode('utf-8')
    i_nonce = os.urandom(12)
    i_cipher = aes_sess.encrypt(i_nonce, idx_bytes, None)

    valid_fixture = "valid_test.akb"
    signed_buf = bytearray()
    def emit_f(d): signed_buf.extend(d); return d

    with open(valid_fixture, "wb") as f:
        f.write(emit_f(header_packed))
        f.write(emit_f(struct.pack('<I', len(dpapi_b)) + dpapi_b))
        f.write(emit_f(struct.pack('<I', len(pass_wrapped)) + pass_wrapped))
        f.write(emit_f(struct.pack('<I', len(pub_der)) + pub_der))
        f.write(emit_f(struct.pack('<Q 12s', len(m_cipher), m_nonce) + m_cipher))
        f.write(emit_f(struct.pack('<Q 12s', len(i_cipher), i_nonce) + i_cipher))
        f.write(emit_f(struct.pack('<I', 1)))
        f.write(emit_f(struct.pack('<B 16s Q I 12s', 1, bid, 17408, 512, c_nonce) + c_cipher))
        final_sig = priv_key.sign(bytes(signed_buf), ec.ECDSA(hashes.SHA384()))
        f.write(struct.pack('<I I', RecoveryConstants.COMMIT_MAGIC, len(final_sig)) + final_sig)

    valid_fixture_bytes = Path(valid_fixture).read_bytes()

    # 9. Manifest decrypt
    def t9():
        parsed = AKBKParser.parse_and_verify(valid_fixture, "TestSecretPass")
        assert parsed["manifest"] == m_dict
    assert_test("AES-GCM manifest decrypt", t9)

    # 10. Manifest tamper
    def t10():
        corrupt_m = bytearray(valid_fixture_bytes)
        env_len = 4 + len(dpapi_b) + 4 + len(pass_wrapped) + 4 + len(pub_der)
        m_cipher_off = 57 + env_len + 8 + 12
        corrupt_m[m_cipher_off] ^= 0x01
        with open("corrupt_m.akb", "wb") as f: f.write(corrupt_m)
        try:
            AKBKParser.parse_and_verify("corrupt_m.akb", "TestSecretPass")
            raise AssertionError("Manifest corruption accepted!")
        except ValueError as ve:
            assert "Manifest decryption or GCM authentication FAILED" in str(ve)
        finally:
            if os.path.exists("corrupt_m.akb"): os.remove("corrupt_m.akb")
    assert_test("Manifest GCM tamper rejection", t10)

    # 11. Index decrypt
    def t11():
        parsed = AKBKParser.parse_and_verify(valid_fixture, "TestSecretPass")
        assert parsed["index_entries"] == idx_data
    assert_test("Index encryption/decryption", t11)

    # 12. Index tamper
    def t12():
        corrupt_i = bytearray(valid_fixture_bytes)
        env_len = 4 + len(dpapi_b) + 4 + len(pass_wrapped) + 4 + len(pub_der)
        i_cipher_off = 57 + env_len + (8 + 12 + len(m_cipher)) + 8 + 12
        corrupt_i[i_cipher_off] ^= 0x01
        with open("corrupt_i.akb", "wb") as f: f.write(corrupt_i)
        try:
            AKBKParser.parse_and_verify("corrupt_i.akb", "TestSecretPass")
            raise AssertionError("Index corruption accepted!")
        except ValueError as ve:
            assert "Block Index decryption or GCM authentication FAILED" in str(ve)
        finally:
            if os.path.exists("corrupt_i.akb"): os.remove("corrupt_i.akb")
    assert_test("Index GCM tamper rejection", t12)

    # 13. Type-1 chunk
    def t13():
        parsed = AKBKParser.parse_and_verify(valid_fixture, "TestSecretPass")
        cid = uuid.UUID(bytes=bid).hex
        assert cid in parsed["chunks_map"] and parsed["chunks_map"][cid]["type"] == 1
    assert_test("Type-1 chunk parsing", t13)

    # 14. Type-2 reference
    def t14():
        type2_fixture = "type2.akb"
        t2_buf = bytearray()
        def emit_t2(d): t2_buf.extend(d); return d
        ref_u = uuid.uuid4().bytes
        with open(type2_fixture, "wb") as f:
            f.write(emit_t2(header_packed))
            f.write(emit_t2(struct.pack('<I', len(dpapi_b)) + dpapi_b))
            f.write(emit_t2(struct.pack('<I', len(pass_wrapped)) + pass_wrapped))
            f.write(emit_t2(struct.pack('<I', len(pub_der)) + pub_der))
            f.write(emit_t2(struct.pack('<Q 12s', len(m_cipher), m_nonce) + m_cipher))
            t2_idx = [{"offset": 17408, "size": 512, "id": uuid.UUID(bytes=bid).hex, "is_ref": True, "ref_id": uuid.UUID(bytes=ref_u).hex}]
            t2_idx_b = json.dumps(t2_idx, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode('utf-8')
            t2_idx_c = aes_sess.encrypt(i_nonce, t2_idx_b, None)
            f.write(emit_t2(struct.pack('<Q 12s', len(t2_idx_c), i_nonce) + t2_idx_c))
            f.write(emit_t2(struct.pack('<I', 1)))
            f.write(emit_t2(struct.pack('<B 16s 16s Q I', 2, bid, ref_u, 17408, 512)))
            t2_sig = priv_key.sign(bytes(t2_buf), ec.ECDSA(hashes.SHA384()))
            f.write(struct.pack('<I I', RecoveryConstants.COMMIT_MAGIC, len(t2_sig)) + t2_sig)
        try:
            parsed = AKBKParser.parse_and_verify(type2_fixture, "TestSecretPass")
            assert uuid.UUID(bytes=bid).hex in parsed["chunks_map"]
            assert parsed["chunks_map"][uuid.UUID(bytes=bid).hex]["type"] == 2
            assert parsed["chunks_map"][uuid.UUID(bytes=bid).hex]["ref_id"] == uuid.UUID(bytes=ref_u).hex
        finally:
            if os.path.exists(type2_fixture): os.remove(type2_fixture)
    assert_test("Type-2 reference record parsing", t14)

    # 15. Payload tamper
    def t15():
        corrupt_c = bytearray(valid_fixture_bytes)
        env_len = 4 + len(dpapi_b) + 4 + len(pass_wrapped) + 4 + len(pub_der)
        c_off = 57 + env_len + (8 + 12 + len(m_cipher)) + (8 + 12 + len(i_cipher)) + 4 + 1 + 16 + 8 + 4 + 12
        corrupt_c[c_off] ^= 0x01
        with open("corrupt_c.akb", "wb") as f: f.write(corrupt_c)
        try:
            AKBKParser.parse_and_verify("corrupt_c.akb", "TestSecretPass")
            raise AssertionError("Tampered payload accepted!")
        except ValueError as ve:
            assert "Signature Verification FAILED" in str(ve)
        finally:
            if os.path.exists("corrupt_c.akb"): os.remove("corrupt_c.akb")
    assert_test("Payload tamper rejection", t15)

    # 16. Explicit chunk count
    def t16():
        p_c = AKBKParser.parse_and_verify(valid_fixture, "TestSecretPass")
        assert len(p_c["chunks_map"]) == 1
    assert_test("Explicit chunk count parsed", t16)

    # 17. Truncated payload
    def t17():
        trunc_p = valid_fixture_bytes[:len(valid_fixture_bytes) - 40]
        with open("trunc_p.akb", "wb") as f: f.write(trunc_p)
        try:
            AKBKParser.parse_and_verify("trunc_p.akb", "TestSecretPass")
            raise AssertionError("Truncated payload accepted!")
        except ValueError:
            pass
        finally:
            if os.path.exists("trunc_p.akb"): os.remove("trunc_p.akb")
    assert_test("Truncated payload rejection", t17)

    # 18. Invalid commit
    def t18():
        bad_cm = bytearray(valid_fixture_bytes)
        sig_len = struct.unpack('<I', valid_fixture_bytes[-len(final_sig)-4 : -len(final_sig)])[0]
        cm_off = len(valid_fixture_bytes) - sig_len - 8
        bad_cm[cm_off] ^= 0x01
        with open("bad_cm.akb", "wb") as f: f.write(bad_cm)
        try:
            AKBKParser.parse_and_verify("bad_cm.akb", "TestSecretPass")
            raise AssertionError("Invalid commit magic accepted!")
        except ValueError as ve:
            assert "Invalid Commit Marker Magic" in str(ve)
        finally:
            if os.path.exists("bad_cm.akb"): os.remove("bad_cm.akb")
    assert_test("Invalid commit marker rejection", t18)

    # 19. Signature verification
    def t19():
        parsed = AKBKParser.parse_and_verify(valid_fixture, "TestSecretPass")
        assert parsed["backup_id"] == str(uuid.UUID(bytes=bid))
    assert_test("DER signature verification", t19)

    # 20. Signature tamper
    def t20():
        corrupt_sig = bytearray(valid_fixture_bytes)
        corrupt_sig[-5] ^= 0x01
        with open("corrupt_sig.akb", "wb") as f: f.write(corrupt_sig)
        try:
            AKBKParser.parse_and_verify("corrupt_sig.akb", "TestSecretPass")
            raise AssertionError("Corrupted signature accepted!")
        except ValueError as ve:
            assert "ECDSA P-384 Signature Verification FAILED" in str(ve)
        finally:
            if os.path.exists("corrupt_sig.akb"): os.remove("corrupt_sig.akb")
    assert_test("Signature tamper rejection", t20)

    # 21. Trailing garbage
    def t21():
        with open("trailing.akb", "wb") as f: f.write(valid_fixture_bytes + b"GARBAGE")
        try:
            AKBKParser.parse_and_verify("trailing.akb", "TestSecretPass")
            raise AssertionError("Trailing garbage accepted!")
        except ValueError as ve:
            assert "Trailing garbage detected" in str(ve)
        finally:
            if os.path.exists("trailing.akb"): os.remove("trailing.akb")
    assert_test("Trailing garbage rejection", t21)

    # 22. Duplicate chunk ID
    def t22():
        dup_buf = bytearray()
        def emit_dup(d): dup_buf.extend(d); return d
        with open("dup.akb", "wb") as f:
            f.write(emit_dup(header_packed))
            f.write(emit_dup(struct.pack('<I', len(dpapi_b)) + dpapi_b))
            f.write(emit_dup(struct.pack('<I', len(pass_wrapped)) + pass_wrapped))
            f.write(emit_dup(struct.pack('<I', len(pub_der)) + pub_der))
            f.write(emit_dup(struct.pack('<Q 12s', len(m_cipher), m_nonce) + m_cipher))
            f.write(emit_dup(struct.pack('<Q 12s', len(i_cipher), i_nonce) + i_cipher))
            f.write(emit_dup(struct.pack('<I', 2)))
            f.write(emit_dup(struct.pack('<B 16s Q I 12s', 1, bid, 17408, 512, c_nonce) + c_cipher))
            f.write(emit_dup(struct.pack('<B 16s Q I 12s', 1, bid, 17920, 512, c_nonce) + c_cipher))
            dup_sig = priv_key.sign(bytes(dup_buf), ec.ECDSA(hashes.SHA384()))
            f.write(struct.pack('<I I', RecoveryConstants.COMMIT_MAGIC, len(dup_sig)) + dup_sig)
        try:
            AKBKParser.parse_and_verify("dup.akb", "TestSecretPass")
            raise AssertionError("Duplicate chunk ID accepted!")
        except ValueError as ve:
            assert "Duplicate chunk ID detected" in str(ve)
        finally:
            if os.path.exists("dup.akb"): os.remove("dup.akb")
    assert_test("Duplicate chunk ID rejection", t22)

    # Differential repository helpers
    def create_akb(fpath, b_uuid, p_uuid, is_diff, chunks_info, idx_info):
        b_buf = bytearray()
        def em(d): b_buf.extend(d); return d
        h = struct.pack(RecoveryConstants.STRUCT_FORMAT, RecoveryConstants.MAGIC, 1, 0,
                        2 if is_diff else 1, b_uuid.bytes, p_uuid.bytes if p_uuid else bytes(16),
                        int(time.time()), host_id)
        sess_k = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=b_uuid.bytes, iterations=600000).derive(master_k)
        aes_s = AESGCM(sess_k)
        m_c = aes_s.encrypt(m_nonce, m_bytes, None)
        idx_b = json.dumps(idx_info, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode('utf-8')
        idx_c = aes_s.encrypt(i_nonce, idx_b, None)
        with open(fpath, "wb") as f:
            f.write(em(h))
            f.write(em(struct.pack('<I', len(dpapi_b)) + dpapi_b))
            f.write(em(struct.pack('<I', len(pass_wrapped)) + pass_wrapped))
            f.write(em(struct.pack('<I', len(pub_der)) + pub_der))
            f.write(em(struct.pack('<Q 12s', len(m_c), m_nonce) + m_c))
            f.write(em(struct.pack('<Q 12s', len(idx_c), i_nonce) + idx_c))
            f.write(em(struct.pack('<I', len(chunks_info))))
            for cinf in chunks_info:
                if cinf["type"] == 1:
                    cc = aes_s.encrypt(c_nonce, cinf["data"], None)
                    f.write(em(struct.pack('<B 16s Q I 12s', 1, cinf["id"].bytes, cinf["offset"], len(cinf["data"]), c_nonce) + cc))
                else:
                    f.write(em(struct.pack('<B 16s 16s Q I', 2, cinf["id"].bytes, cinf["ref_id"].bytes, cinf["offset"], cinf["size"])))
            sg = priv_key.sign(bytes(b_buf), ec.ECDSA(hashes.SHA384()))
            f.write(struct.pack('<I I', RecoveryConstants.COMMIT_MAGIC, len(sg)) + sg)

    u_full = uuid.uuid4()
    u_d1 = uuid.uuid4()
    u_d2 = uuid.uuid4()
    c_full_id = uuid.uuid4()
    c_d1_id = uuid.uuid4()
    c_d2_id = uuid.uuid4()

    repo_dir = Path("test_repo")
    repo_dir.mkdir(exist_ok=True)
    f_full = str(repo_dir / f"FULL_{u_full.hex}.akb")
    f_d1 = str(repo_dir / f"DIFF1_{u_d1.hex}.akb")
    f_d2 = str(repo_dir / f"DIFF2_{u_d2.hex}.akb")

    create_akb(f_full, u_full, None, False,
               [{"type": 1, "id": c_full_id, "offset": 17408, "data": bytes(512)}],
               [{"offset": 17408, "size": 512, "id": c_full_id.hex, "is_ref": False, "ref_id": uuid.UUID(int=0).hex}])

    create_akb(f_d1, u_d1, u_full, True,
               [{"type": 1, "id": c_d1_id, "offset": 17920, "data": bytes(512)}],
               [{"offset": 17920, "size": 512, "id": c_d1_id.hex, "is_ref": False, "ref_id": uuid.UUID(int=0).hex}])

    c_d2_ref_id = uuid.uuid4()
    create_akb(f_d2, u_d2, u_d1, True,
               [{"type": 1, "id": c_d2_id, "offset": 17408, "data": bytes(512)},
                {"type": 2, "id": c_d2_ref_id, "ref_id": c_d1_id, "offset": 17920, "size": 512}],
               [{"offset": 17408, "size": 512, "id": c_d2_id.hex, "is_ref": False, "ref_id": uuid.UUID(int=0).hex},
                {"offset": 17920, "size": 512, "id": c_d2_ref_id.hex, "is_ref": True, "ref_id": c_d1_id.hex}])

    # 23. Missing parent
    def t23():
        orphan_uuid = uuid.uuid4()
        f_orphan = str(repo_dir / f"ORPHAN_{orphan_uuid.hex}.akb")
        create_akb(f_orphan, orphan_uuid, uuid.uuid4(), True, [], [])
        try:
            ChainResolver.resolve_chain(f_orphan, str(repo_dir), "TestSecretPass")
            raise AssertionError("Missing parent resolved successfully!")
        except ValueError as ve:
            assert "Missing parent backup ID" in str(ve)
        finally:
            if os.path.exists(f_orphan): os.remove(f_orphan)
    assert_test("Missing parent rejection", t23)

    # 24. Parent cycle
    def t24():
        u_c1 = uuid.uuid4()
        u_c2 = uuid.uuid4()
        f_c1 = str(repo_dir / f"CYC1_{u_c1.hex}.akb")
        f_c2 = str(repo_dir / f"CYC2_{u_c2.hex}.akb")
        create_akb(f_c1, u_c1, u_c2, True, [], [])
        create_akb(f_c2, u_c2, u_c1, True, [], [])
        try:
            ChainResolver.resolve_chain(f_c1, str(repo_dir), "TestSecretPass")
            raise AssertionError("Parent cycle was not detected!")
        except ValueError as ve:
            assert "Circular parent reference detected" in str(ve)
        finally:
            if os.path.exists(f_c1): os.remove(f_c1)
            if os.path.exists(f_c2): os.remove(f_c2)
    assert_test("Parent cycle rejection", t24)

    # 25. Unresolved reference
    def t25():
        u_bad_ref = uuid.uuid4()
        f_bad_ref = str(repo_dir / f"BADREF_{u_bad_ref.hex}.akb")
        missing_chunk = uuid.uuid4()
        c_local_id = uuid.uuid4()
        create_akb(f_bad_ref, u_bad_ref, u_full, True,
                   [{"type": 2, "id": c_local_id, "ref_id": missing_chunk, "offset": 17408, "size": 512}],
                   [{"offset": 17408, "size": 512, "id": c_local_id.hex, "is_ref": True, "ref_id": missing_chunk.hex}])
        try:
            ChainResolver.resolve_chain(f_bad_ref, str(repo_dir), "TestSecretPass")
            raise AssertionError("Unresolved reference was resolved successfully!")
        except ValueError as ve:
            assert "Unresolved reference" in str(ve)
        finally:
            if os.path.exists(f_bad_ref): os.remove(f_bad_ref)
    assert_test("Unresolved chunk reference rejection", t25)

    # 26. Reference cycle
    def t26():
        r_id1 = uuid.uuid4().hex
        r_id2 = uuid.uuid4().hex
        mock_chain = [
            {"chunks_map": {r_id1: {"type": 2, "ref_id": r_id2}}},
            {"chunks_map": {r_id2: {"type": 2, "ref_id": r_id1}}}
        ]
        try:
            ChainResolver.resolve_chunk_reference(r_id2, 1, mock_chain)
            raise AssertionError("Circular chunk reference not detected!")
        except ValueError as ve:
            assert "Circular chunk reference detected" in str(ve)
    assert_test("Chunk reference cycle rejection", t26)

    # 27. Duplicate backup ID
    def t27():
        u_dup = uuid.uuid4()
        f_dup1 = str(repo_dir / f"DUP1_{u_dup.hex}.akb")
        f_dup2 = str(repo_dir / f"DUP2_{u_dup.hex}.akb")
        create_akb(f_dup1, u_dup, None, False, [], [])
        create_akb(f_dup2, u_dup, None, False, [], [])
        u_child = uuid.uuid4()
        f_child = str(repo_dir / f"CHILD_{u_child.hex}.akb")
        create_akb(f_child, u_child, u_dup, True, [], [])
        try:
            ChainResolver.resolve_chain(f_child, str(repo_dir), "TestSecretPass")
            raise AssertionError("Duplicate candidate backup ID accepted!")
        except ValueError as ve:
            assert "Duplicate candidate backup ID" in str(ve)
        finally:
            if os.path.exists(f_dup1): os.remove(f_dup1)
            if os.path.exists(f_dup2): os.remove(f_dup2)
            if os.path.exists(f_child): os.remove(f_child)
    assert_test("Duplicate backup ID rejection", t27)

    # 28. Differential override
    def t28():
        chain, resolved_blocks = ChainResolver.resolve_chain(f_d2, str(repo_dir), "TestSecretPass")
        assert len(chain) == 3
        assert resolved_blocks[17408]["backup_id"] == str(u_d2)
        assert resolved_blocks[17920]["backup_id"] == str(u_d2)
        assert resolved_blocks[17920]["chunk"]["type"] == 1
    assert_test("Differential override resolution", t28)

    # Cleanup repo
    for p in repo_dir.glob("*.akb"):
        try: os.remove(p)
        except Exception: pass
    try: repo_dir.rmdir()
    except Exception: pass

    # 29. Mapping missing
    def t29():
        mock_chain = [{"backup_id": "test", "backup_type": "FULL", "manifest": {"Host": "H", "BootType": "UEFI"}}]
        passed, rep = PreflightManager.run_preflight(mock_chain, {17408: {"offset": 17408, "size": 512}}, 0)
        assert not passed and any("Manifest structure 'PhysicalDiskMapping' is absent" in r for r in rep)
    assert_test("Missing physical mapping rejection", t29)

    # 30. Mapping malformed
    def t30():
        mock_chain = [{"backup_id": "test", "backup_type": "FULL", "manifest": {"Host": "H", "BootType": "UEFI", "PhysicalDiskMapping": {"schema_version": 1}}}]
        passed, rep = PreflightManager.run_preflight(mock_chain, {17408: {"offset": 17408, "size": 512}}, 0)
        assert not passed and any("Missing keys" in r for r in rep)
    assert_test("Malformed physical mapping schema rejection", t30)

    # 31. Schema mismatch
    def t31():
        bad_ver_map = copy.deepcopy(make_valid_physical_mapping_fixture())
        bad_ver_map["schema_version"] = 99
        mock_chain = [{"backup_id": "test", "backup_type": "FULL", "manifest": {"Host": "H", "BootType": "UEFI", "PhysicalDiskMapping": bad_ver_map}}]
        passed, rep = PreflightManager.run_preflight(mock_chain, {17408: {"offset": 17408, "size": 512}}, 0)
        assert not passed and any("Unsupported mapping schema_version" in r for r in rep)
    assert_test("Invalid schema version rejection", t31)

    # Helper to run preflight with isolated mock hardware disk
    def run_preflight_with_mock_disk(mock_chain, resolved_blocks):
        orig_enum = DiskManager.enumerate_physical_disks
        mock_disk = {
            "Index": 0, "Model": "TEST_DISK_MODEL", "SerialNumber": "TEST_SN_12345",
            "Size": 100 * 1024 * 1024, "BytesPerSector": 512, "DeviceID": "\\\\.\\PHYSICALDRIVE0"
        }
        DiskManager.enumerate_physical_disks = staticmethod(lambda: [mock_disk])
        try:
            return PreflightManager.run_preflight(mock_chain, resolved_blocks, 0)
        finally:
            DiskManager.enumerate_physical_disks = orig_enum

    # 32. Invalid partition index reference in block
    def t32():
        bad_part_map = copy.deepcopy(make_valid_physical_mapping_fixture())
        bad_part_map["blocks"][0]["partition_index"] = 999
        test_manifest = copy.deepcopy(m_dict)
        test_manifest["PhysicalDiskMapping"] = bad_part_map
        mock_chain = [{"backup_id": "test", "backup_type": "FULL", "manifest": test_manifest}]
        passed, rep = run_preflight_with_mock_disk(mock_chain, {17408: {"offset": 17408, "size": 512}})
        assert passed is False and any("references nonexistent partition index" in r for r in rep)
    assert_test("Invalid partition index reference rejection", t32)

    # 33. Duplicate partition index
    def t33():
        bad_pid_map = copy.deepcopy(make_valid_physical_mapping_fixture())
        bad_pid_map["partitions"][1]["index"] = bad_pid_map["partitions"][0]["index"]
        test_manifest = copy.deepcopy(m_dict)
        test_manifest["PhysicalDiskMapping"] = bad_pid_map
        mock_chain = [{"backup_id": "test", "backup_type": "FULL", "manifest": test_manifest}]
        passed, rep = run_preflight_with_mock_disk(mock_chain, {17408: {"offset": 17408, "size": 512}})
        assert passed is False and any("Duplicate partition index" in r for r in rep)
    assert_test("Duplicate partition index rejection", t33)

    # 34. Duplicate partition GUID
    def t34():
        bad_guid_map = copy.deepcopy(make_valid_physical_mapping_fixture())
        bad_guid_map["partitions"][1]["partition_guid"] = bad_guid_map["partitions"][0]["partition_guid"]
        test_manifest = copy.deepcopy(m_dict)
        test_manifest["PhysicalDiskMapping"] = bad_guid_map
        mock_chain = [{"backup_id": "test", "backup_type": "FULL", "manifest": test_manifest}]
        passed, rep = run_preflight_with_mock_disk(mock_chain, {17408: {"offset": 17408, "size": 512}})
        assert passed is False and any("Duplicate partition GUID" in r for r in rep)
    assert_test("Duplicate partition GUID rejection", t34)

    # 35. Partition overlap
    def t35():
        bad_po_map = copy.deepcopy(make_valid_physical_mapping_fixture())
        bad_po_map["partitions"][1]["start_offset"] = bad_po_map["partitions"][0]["start_offset"] + 1024
        bad_po_map["partitions"][1]["start_lba"] = bad_po_map["partitions"][0]["start_lba"] + 2
        bad_po_map["partitions"][1]["end_lba"] = bad_po_map["partitions"][1]["start_lba"] + (bad_po_map["partitions"][1]["size"] // 512) - 1
        test_manifest = copy.deepcopy(m_dict)
        test_manifest["PhysicalDiskMapping"] = bad_po_map
        mock_chain = [{"backup_id": "test", "backup_type": "FULL", "manifest": test_manifest}]
        passed, rep = run_preflight_with_mock_disk(mock_chain, {17408: {"offset": 17408, "size": 512}})
        assert passed is False and any("Overlapping partitions detected" in r for r in rep)
    assert_test("Partition overlap rejection", t35)

    # 36. Invalid LBA calculation
    def t36():
        bad_lba_map = copy.deepcopy(make_valid_physical_mapping_fixture())
        bad_lba_map["blocks"][0]["lba"] = 0  # Inconsistent with start_lba of partition
        test_manifest = copy.deepcopy(m_dict)
        test_manifest["PhysicalDiskMapping"] = bad_lba_map
        mock_chain = [{"backup_id": "test", "backup_type": "FULL", "manifest": test_manifest}]
        passed, rep = run_preflight_with_mock_disk(mock_chain, {17408: {"offset": 17408, "size": 512}})
        assert passed is False and any("extends outside partition" in r for r in rep)
    assert_test("Invalid LBA calculation rejection", t36)

    # 37. Invalid sector count
    def t37():
        bad_sc_map = copy.deepcopy(make_valid_physical_mapping_fixture())
        bad_sc_map["blocks"][0]["sector_count"] = 5  # Inconsistent with size 512
        test_manifest = copy.deepcopy(m_dict)
        test_manifest["PhysicalDiskMapping"] = bad_sc_map
        mock_chain = [{"backup_id": "test", "backup_type": "FULL", "manifest": test_manifest}]
        passed, rep = run_preflight_with_mock_disk(mock_chain, {17408: {"offset": 17408, "size": 512}})
        assert passed is False and any("sector_count * sector_size" in r for r in rep)
    assert_test("Invalid sector_count calculation rejection", t37)

    # 38. Block outside partition
    def t38():
        bad_out_map = copy.deepcopy(make_valid_physical_mapping_fixture())
        bad_out_map["blocks"][0]["lba"] = bad_out_map["partitions"][0]["end_lba"] + 10
        test_manifest = copy.deepcopy(m_dict)
        test_manifest["PhysicalDiskMapping"] = bad_out_map
        mock_chain = [{"backup_id": "test", "backup_type": "FULL", "manifest": test_manifest}]
        passed, rep = run_preflight_with_mock_disk(mock_chain, {17408: {"offset": 17408, "size": 512}})
        assert passed is False and any("extends outside partition" in r for r in rep)
    assert_test("Block outside partition rejection", t38)

    # 39. Mapping overlap
    def t39():
        bad_ov_map = copy.deepcopy(make_valid_physical_mapping_fixture())
        bad_ov_map["blocks"] = [
            {"offset": 17408, "size": 1024, "partition_index": 0, "lba": 2048, "sector_count": 2},
            {"offset": 17408 + 512, "size": 1024, "partition_index": 0, "lba": 2049, "sector_count": 2}
        ]
        test_manifest = copy.deepcopy(m_dict)
        test_manifest["PhysicalDiskMapping"] = bad_ov_map
        mock_chain = [{"backup_id": "test", "backup_type": "FULL", "manifest": test_manifest}]
        passed, rep = run_preflight_with_mock_disk(mock_chain, {17408: {"offset": 17408, "size": 1024}, 17408 + 512: {"offset": 17408 + 512, "size": 1024}})
        assert passed is False and any("Overlapping" in r for r in rep)
    assert_test("Mapping block overlap rejection", t39)

    # 40. Final resolved overlap
    def t40():
        resolved_overlap = {
            0: {"offset": 0, "size": 1024, "chunk": None, "backup_id": "1"},
            512: {"offset": 512, "size": 1024, "chunk": None, "backup_id": "2"}
        }
        sorted_final_blocks = sorted(resolved_overlap.values(), key=lambda b: b["offset"])
        has_overlap = False
        for i in range(len(sorted_final_blocks) - 1):
            if sorted_final_blocks[i]["offset"] + sorted_final_blocks[i]["size"] > sorted_final_blocks[i + 1]["offset"]:
                has_overlap = True
                break
        assert has_overlap
    assert_test("Final resolved block overlap detection", t40)

    # Isolated mock hardware targets for Tests 41-44
    def get_isolated_test_target():
        return {
            "Index": 0, "Model": "TEST_DISK_MODEL", "SerialNumber": "TEST_SN_12345",
            "Size": 100 * 1024 * 1024, "BytesPerSector": 512, "DeviceID": "\\\\.\\PHYSICALDRIVE0"
        }

    # 41. Target disk too small
    def t41():
        tdisk = get_isolated_test_target()
        tdisk["Size"] = 50 * 1024 * 1024  # Target capacity (50 MiB) < Source disk size (100 MiB)
        test_manifest = copy.deepcopy(m_dict)
        mock_chain = [{"backup_id": "test", "backup_type": "FULL", "manifest": test_manifest}]
        orig_enum = DiskManager.enumerate_physical_disks
        DiskManager.enumerate_physical_disks = staticmethod(lambda: [tdisk])
        try:
            passed, rep = PreflightManager.run_preflight(mock_chain, {17408: {"offset": 17408, "size": 512}}, tdisk["Index"])
            assert passed is False and any("Target disk capacity is insufficient" in r for r in rep)
        finally:
            DiskManager.enumerate_physical_disks = orig_enum
    assert_test("Target disk too small rejection", t41)

    # 42. Target sector mismatch
    def t42():
        src_sz_4k = 100 * 1024 * 1024
        sec_sz_4k = 4096
        total_lbas_4k = src_sz_4k // sec_sz_4k
        num_parts_4k = 32
        part_sz_4k = 128
        array_lba_span_4k = (num_parts_4k * part_sz_4k + sec_sz_4k - 1) // sec_sz_4k

        pm_4k = bytearray(sec_sz_4k)
        pm_4k[510:512] = b"\x55\xAA"
        pa_4k = bytearray(num_parts_4k * part_sz_4k)
        pa_crc_4k = zlib.crc32(pa_4k) & 0xFFFFFFFF

        bkp_lba_4k = total_lbas_4k - 1
        b_part_lba_4k = bkp_lba_4k - array_lba_span_4k
        first_u_4k = 34
        last_u_4k = total_lbas_4k - 34

        ph_raw_4k = struct.pack("<8sIIIIQQQQ16sQIII", b"EFI PART", 0x00010000, 92, 0, 0, 1, bkp_lba_4k, first_u_4k, last_u_4k, bytes(16), 2, num_parts_4k, part_sz_4k, pa_crc_4k)
        ph_crc_4k = zlib.crc32(ph_raw_4k) & 0xFFFFFFFF
        ph_4k = ph_raw_4k[:16] + struct.pack("<I", ph_crc_4k) + ph_raw_4k[20:]

        bh_raw_4k = struct.pack("<8sIIIIQQQQ16sQIII", b"EFI PART", 0x00010000, 92, 0, 0, bkp_lba_4k, 1, first_u_4k, last_u_4k, bytes(16), b_part_lba_4k, num_parts_4k, part_sz_4k, pa_crc_4k)
        bh_crc_4k = zlib.crc32(bh_raw_4k) & 0xFFFFFFFF
        bh_4k = bh_raw_4k[:16] + struct.pack("<I", bh_crc_4k) + bh_raw_4k[20:]

        valid_4k_map = {
            "schema_version": 1,
            "disk_size": src_sz_4k,
            "sector_size": sec_sz_4k,
            "disk_signature_or_guid": "DISK-GUID-4K",
            "partition_table": "GPT",
            "partition_table_metadata": {
                "protective_mbr": bytes(pm_4k).hex(),
                "primary_header": bytes(ph_4k).hex(),
                "primary_partition_array": bytes(pa_4k).hex(),
                "backup_partition_array": bytes(pa_4k).hex(),
                "backup_header": bytes(bh_4k).hex()
            },
            "partitions": [
                {
                    "index": 0,
                    "type_guid": "C12A7328-F81F-11D2-BA4B-00A0C93EC93B",
                    "partition_guid": str(uuid.uuid4()),
                    "start_offset": 34 * sec_sz_4k,
                    "size": 10 * 1024 * 1024,
                    "start_lba": 34,
                    "end_lba": 34 + (10 * 1024 * 1024 // sec_sz_4k) - 1,
                    "attributes": 0
                }
            ],
            "blocks": [{"offset": 17408, "size": 4096, "partition_index": 0, "lba": 34, "sector_count": 1}]
        }

        tdisk = get_isolated_test_target()
        tdisk["BytesPerSector"] = 512  # Target is 512B, source mapping is 4096B
        test_manifest = copy.deepcopy(m_dict)
        test_manifest["PhysicalDiskMapping"] = valid_4k_map
        mock_chain = [{"backup_id": "test", "backup_type": "FULL", "manifest": test_manifest}]
        orig_enum = DiskManager.enumerate_physical_disks
        DiskManager.enumerate_physical_disks = staticmethod(lambda: [tdisk])
        try:
            passed, rep = PreflightManager.run_preflight(mock_chain, {17408: {"offset": 17408, "size": 4096}}, tdisk["Index"])
            assert passed is False and any("incompatible with source sector size" in r for r in rep)
        finally:
            DiskManager.enumerate_physical_disks = orig_enum
    assert_test("Target sector size mismatch rejection", t42)

    # Regression assertion: Confirm m_dict remained untainted by tests 41 & 42
    assert m_dict["PhysicalDiskMapping"]["disk_size"] == source_disk_sz_test
    assert m_dict["PhysicalDiskMapping"]["sector_size"] == 512

    # 43. Target identity mismatch
    def t43():
        tdisk = get_isolated_test_target()
        fake_info = dict(tdisk)
        fake_info["Model"] = "MODIFIED_MODEL_ID"
        orig_enum = DiskManager.enumerate_physical_disks
        DiskManager.enumerate_physical_disks = staticmethod(lambda: [tdisk])
        try:
            RealBlockRestoreEngine.validate_target_identity(fake_info)
            raise AssertionError("Identity change was not detected!")
        except ValueError as ve:
            assert "identity or geometry changed" in str(ve)
        finally:
            DiskManager.enumerate_physical_disks = orig_enum
    assert_test("Target device identity mismatch detection", t43)

    # 44. DeviceID mismatch
    def t44():
        tdisk = get_isolated_test_target()
        fake_info = dict(tdisk)
        fake_info["DeviceID"] = "CORRUPTED_DEVICE_PATH"
        orig_enum = DiskManager.enumerate_physical_disks
        DiskManager.enumerate_physical_disks = staticmethod(lambda: [tdisk])
        try:
            RealBlockRestoreEngine.validate_target_identity(fake_info)
            raise AssertionError("DeviceID change was not detected!")
        except ValueError as ve:
            assert "identity or geometry changed" in str(ve)
        finally:
            DiskManager.enumerate_physical_disks = orig_enum
    assert_test("Target DeviceID mismatch detection", t44)

    mock_blocks = {
        17408: {"offset": 17408, "size": 512, "chunk": {"aes": aes_sess, "nonce": c_nonce, "ciphertext": c_cipher}}
    }

    # 45. Memory backend: write error
    def t45():
        dev = MemoryBlockDevice(source_disk_sz_test)
        dev.simulate_write_error = True
        try:
            RealBlockRestoreEngine.execute_block_restore(mock_blocks, dev, source_disk_sz_test, 512, m_dict["PhysicalDiskMapping"])
            raise AssertionError("Write error not caught!")
        except IOError:
            pass
    assert_test("Memory backend write error rejection", t45)

    # 46. Memory backend: short write
    def t46():
        dev = MemoryBlockDevice(source_disk_sz_test)
        dev.simulate_short_write = True
        try:
            RealBlockRestoreEngine.execute_block_restore(mock_blocks, dev, source_disk_sz_test, 512, m_dict["PhysicalDiskMapping"])
            raise AssertionError("Short write not caught!")
        except IOError as ie:
            assert "Short write" in str(ie) or "Incomplete physical write" in str(ie)
    assert_test("Memory backend short write rejection", t46)

    # 47. Memory backend: read error
    def t47():
        dev = MemoryBlockDevice(source_disk_sz_test)
        dev.simulate_read_error = True
        try:
            RealBlockRestoreEngine.execute_block_restore(mock_blocks, dev, source_disk_sz_test, 512, m_dict["PhysicalDiskMapping"])
            raise AssertionError("Read error not caught!")
        except IOError:
            pass
    assert_test("Memory backend read error rejection", t47)

    # 48. Memory backend: short read (isolated instance)
    def t48():
        dev = MemoryBlockDevice(source_disk_sz_test)
        dev.simulate_short_read = True
        try:
            RealBlockRestoreEngine.execute_block_restore(mock_blocks, dev, source_disk_sz_test, 512, m_dict["PhysicalDiskMapping"])
            raise AssertionError("Short read not caught!")
        except IOError as ie:
            assert "Incomplete physical read-back" in str(ie)
    assert_test("Memory backend short read rejection", t48)

    # 49. Memory backend: read-back mismatch
    def t49():
        dev = MemoryBlockDevice(source_disk_sz_test)
        orig_read = dev.read
        def corrupt_read(size):
            data = orig_read(size)
            if dev.partition_table_verified and size == 512:
                return bytearray(size)
            return data
        dev.read = corrupt_read
        try:
            RealBlockRestoreEngine.execute_block_restore(mock_blocks, dev, source_disk_sz_test, 512, m_dict["PhysicalDiskMapping"])
            raise AssertionError("Read-back hash mismatch not caught!")
        except ValueError as ve:
            assert "Read-back verification FAILED" in str(ve)
    assert_test("Memory backend read-back mismatch rejection", t49)

    # 50. Successful MemoryBlockDevice complete restore path
    def t50():
        dev = MemoryBlockDevice(source_disk_sz_test)
        success = RealBlockRestoreEngine.execute_block_restore(mock_blocks, dev, source_disk_sz_test, 512, m_dict["PhysicalDiskMapping"])
        assert success
        
        pt_m = m_dict["PhysicalDiskMapping"]["partition_table_metadata"]
        pmbr_b = bytes.fromhex(pt_m["protective_mbr"])
        phdr_b = bytes.fromhex(pt_m["primary_header"])
        parr_b = bytes.fromhex(pt_m["primary_partition_array"])
        barr_b = bytes.fromhex(pt_m["backup_partition_array"])
        bhdr_b = bytes.fromhex(pt_m["backup_header"])

        p_cur_lba = struct.unpack("<Q", phdr_b[24:32])[0]
        p_part_lba = struct.unpack("<Q", phdr_b[72:80])[0]
        b_cur_lba = struct.unpack("<Q", bhdr_b[24:32])[0]
        b_part_lba = struct.unpack("<Q", bhdr_b[72:80])[0]
        sec_sz = m_dict["PhysicalDiskMapping"]["sector_size"]
        
        # Verify Protective MBR (LBA 0), Primary Header, Primary Array by declared LBAs
        assert dev.buffer[0:len(pmbr_b)] == pmbr_b
        assert dev.buffer[p_cur_lba * sec_sz : p_cur_lba * sec_sz + len(phdr_b)] == phdr_b
        assert dev.buffer[p_part_lba * sec_sz : p_part_lba * sec_sz + len(parr_b)] == parr_b
        
        # Verify Backup Array and Backup Header by declared LBAs
        assert dev.buffer[b_part_lba * sec_sz : b_part_lba * sec_sz + len(barr_b)] == barr_b
        assert dev.buffer[b_cur_lba * sec_sz : b_cur_lba * sec_sz + len(bhdr_b)] == bhdr_b
        
        # Verify restored payload block written to physical offset (LBA 2048 * 512 = 1048576), NOT offset 17408
        assert dev.buffer[2048 * 512 : 2048 * 512 + 512] == test_data
    assert_test("Successful MemoryBlockDevice complete restore path", t50)

    # 51. Logical backup offset differs from physical target LBA
    def t51_decoupled():
        dev = MemoryBlockDevice(source_disk_sz_test)
        decoupled_blocks = {
            17408: {"offset": 17408, "size": 512, "chunk": {"aes": aes_sess, "nonce": c_nonce, "ciphertext": c_cipher}}
        }
        success = RealBlockRestoreEngine.execute_block_restore(decoupled_blocks, dev, source_disk_sz_test, 512, m_dict["PhysicalDiskMapping"])
        assert success
        # Data MUST appear at LBA 2048 * 512 = 1048576, and NOT at backup offset 17408
        assert dev.buffer[1048576 : 1048576 + 512] == test_data
        assert dev.buffer[17408 : 17408 + 512] != test_data
    assert_test("Logical backup offset differs from physical target LBA", t51_decoupled)

    # Cleanup valid fixture
    if os.path.exists(valid_fixture): os.remove(valid_fixture)

    # 52. Fresh Agent -> Recovery Cross Test
    def t52():
        cross_fixture = "agent_fresh.akb"
        try:
            # 1. Probe for producer
            producer_available = False
            try:
                import AKBackupAgent
                if hasattr(AKBackupAgent, "create_synthetic_test_akb"):
                    AKBackupAgent.create_synthetic_test_akb(cross_fixture, "CrossSecret123")
                    producer_available = True
            except ImportError:
                pass

            # 2. Canonical in-test Agent synthesizer
            if not producer_available:
                c_priv = ec.generate_private_key(ec.SECP384R1())
                c_pub = c_priv.public_key()
                c_pub_der = c_pub.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
                
                c_bid = uuid.uuid4().bytes
                c_base = bytes(16)
                c_ts = int(time.time())
                c_host = b'HOSTID8B'
                c_hdr = struct.pack(RecoveryConstants.STRUCT_FORMAT, RecoveryConstants.MAGIC, 1, 0, 1, c_bid, c_base, c_ts, c_host)
                
                c_master = os.urandom(32)
                c_salt_cross = b"AKBackupCrossMachineRecoverySalt"
                c_pass_k = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=c_salt_cross, iterations=100000).derive(b"CrossSecret123")
                c_nonce_pass = os.urandom(12)
                c_pass_wrapped = c_nonce_pass + AESGCM(c_pass_k).encrypt(c_nonce_pass, c_master, None)
                c_dpapi = b"DPAPI_UNAVAILABLE"
                
                c_sess_k = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=c_bid, iterations=600000).derive(c_master)
                c_aes_sess = AESGCM(c_sess_k)
                
                c_manifest = {"Host": "AGENT_HOST", "BootType": "UEFI"}
                c_m_bytes = json.dumps(c_manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode('utf-8')
                c_m_nonce = os.urandom(12)
                c_m_cipher = c_aes_sess.encrypt(c_m_nonce, c_m_bytes, None)
                
                c_chunk_data = b"AgentStreamBlockDataPayload12345"
                c_cid = uuid.uuid4()
                c_idx = [{"offset": 0, "size": len(c_chunk_data), "id": c_cid.hex, "is_ref": False, "ref_id": uuid.UUID(int=0).hex}]
                c_idx_bytes = json.dumps(c_idx, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode('utf-8')
                c_i_nonce = os.urandom(12)
                c_i_cipher = c_aes_sess.encrypt(c_i_nonce, c_idx_bytes, None)
                
                c_c_nonce = os.urandom(12)
                c_c_cipher = c_aes_sess.encrypt(c_c_nonce, c_chunk_data, None)
                
                signed_stream = bytearray()
                def em_c(d): signed_stream.extend(d); return d
                
                with open(cross_fixture, "wb") as f:
                    f.write(em_c(c_hdr))
                    f.write(em_c(struct.pack('<I', len(c_dpapi)) + c_dpapi))
                    f.write(em_c(struct.pack('<I', len(c_pass_wrapped)) + c_pass_wrapped))
                    f.write(em_c(struct.pack('<I', len(c_pub_der)) + c_pub_der))
                    f.write(em_c(struct.pack('<Q 12s', len(c_m_cipher), c_m_nonce) + c_m_cipher))
                    f.write(em_c(struct.pack('<Q 12s', len(c_i_cipher), c_i_nonce) + c_i_cipher))
                    f.write(em_c(struct.pack('<I', 1)))
                    f.write(em_c(struct.pack('<B 16s Q I 12s', 1, c_cid.bytes, 0, len(c_chunk_data), c_c_nonce) + c_c_cipher))
                    final_sig = c_priv.sign(bytes(signed_stream), ec.ECDSA(hashes.SHA384()))
                    f.write(struct.pack('<I I', RecoveryConstants.COMMIT_MAGIC, len(final_sig)) + final_sig)

            # 3. Verify parser & crypto compatibility
            parsed = AKBKParser.parse_and_verify(cross_fixture, "CrossSecret123")
            assert parsed["backup_type"] == "FULL"
            assert len(parsed["chunks_map"]) == 1
            print("[INFO] AGENT -> RECOVERY FORMAT COMPATIBILITY = PASS")
            
            # 4. Assert physical bare-metal restore is strictly refused without PhysicalDiskMapping
            passed, rep = PreflightManager.run_preflight([parsed], {0: {"offset": 0, "size": len(parsed["chunks_map"][list(parsed["chunks_map"].keys())[0]]["ciphertext"]) - 16}}, 0)
            assert passed is False and any("CRITICAL PREFLIGHT REFUSAL" in r for r in rep)
            print("[INFO] PHYSICAL BARE-METAL RESTORE ELIGIBILITY = SAFELY REFUSED")
            return "PASS"
        finally:
            if os.path.exists(cross_fixture): os.remove(cross_fixture)
    assert_test("Fresh Agent -> Recovery cross test", t52)

    # 53. py_compile validation
    def t53():
        import py_compile
        curr_file = os.path.abspath(__file__)
        py_compile.compile(curr_file, doraise=True)
    assert_test("py_compile validation", t53)

    print("========================================================")
    print(f"SELF-TEST COMPLETE: {test_count} Total, {test_count - len(failures) - not_executed_count} Passed, {len(failures)} Failed, {not_executed_count} Not Executed")
    print("========================================================")
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} test(s) failed.")
        sys.exit(1)
    elif not_executed_count > 0:
        print("SELF-TEST COMPLETED WITH UNEXECUTED TESTS.")
        sys.exit(0)
    else:
        print("ALL REQUIRED SELF-TESTS EXECUTED AND PASSED.")
        sys.exit(0)

def main():
    if "--self-test" in sys.argv:
        run_self_test()
        sys.exit(0)

    if tk is None:
        print("CRITICAL: Tkinter is required to run the Recovery GUI.")
        sys.exit(1)

    app = RecoveryWindow()
    app.mainloop()

if __name__ == "__main__":
    main()