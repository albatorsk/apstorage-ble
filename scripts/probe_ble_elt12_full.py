def normalize_storage_ids(storage_id):
    candidates = [storage_id]
    compact = storage_id.replace(":", "")
    if compact and compact not in candidates:
        candidates.append(compact)
    upper = compact.upper()
    if upper and upper not in candidates:
        candidates.append(upper)
    lower = compact.lower()
    if lower and lower not in candidates:
        candidates.append(lower)
    return candidates
#!/usr/bin/env python3
"""
Probe APstorage ELT-12 via BLE using full Home Assistant protocol (Blufi DH, encrypted JSON).
Requires: bleak, pycryptodome
Usage: python3 probe_ble_elt12_full.py <BLE_MAC>
"""
import asyncio
import json
import sys
import hashlib
import secrets
import struct
import time
from bleak import BleakClient
from Crypto.Cipher import AES

WRITE_CHAR = "0000ff07-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR = "0000ff06-0000-1000-8000-00805f9b34fb"
DEVICE_NAME_CHAR = "00002a00-0000-1000-8000-00805f9b34fb"
BLUFI_MTU = 20
AES_KEY_STR = "E7MiPPrs9v6i3DY3"
AES_IV_STR = "8914934610490056"
BLUFI_DH_P_HEX = (
    "cf5cf5c38419a724957ff5dd323b9c45c3cdd261eb740f69aa94b8bb1a5c9640"
    "9153bd76b24222d03274e4725a5406092e9e82e9135c643cae98132b0d95f7d6"
    "5347c68afc1e677da90e51bbab5f5cf429c291b4ba39c6b2dc5e8c7231e46aa7"
    "728e87664532cdf547be20c9a3fa8342be6e34371a27c06f7dc0edddd2f86373"
)
BLUFI_DH_G = 2
RESPONSE_TIMEOUT = 30


def pad_key_16(key):
    return (key + ("0" * (16 - len(key))))[:16].encode("utf-8")

def aes_cfb_encrypt(key, seq, payload):
    iv = bytes([seq & 0xFF]) + b"\x00" * 15
    return AES.new(key, AES.MODE_CFB, iv=iv, segment_size=128).encrypt(payload)

def aes_cfb_decrypt(key, seq, payload):
    iv = bytes([seq & 0xFF]) + b"\x00" * 15
    return AES.new(key, AES.MODE_CFB, iv=iv, segment_size=128).decrypt(payload)

def crc16_app(seed, data):
    crc = (~seed) & 0xFFFF
    for b in data:
        crc ^= (b & 0xFF) << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return (~crc) & 0xFFFF

def make_cmd(frame_type, subtype):
    return (frame_type & 0x03) | ((subtype & 0x3F) << 2)

_write_seq = [0]

def _next_write_seq():
    _write_seq[0] = (_write_seq[0] + 1) & 0xFF
    return _write_seq[0]

def build_packets(cmd, payload, encrypt, checksum, aes_key, mtu=BLUFI_MTU):
    max_payload = mtu - (8 if checksum else 6)
    if max_payload < 1:
        max_payload = 1
    packets = []
    cursor = 0
    total_len = len(payload)
    if total_len == 0:
        seq = _next_write_seq()
        packets.append(build_single_packet(cmd, seq, b"", encrypt, checksum, False, aes_key))
        return packets
    while cursor < total_len:
        chunk_end = min(total_len, cursor + max_payload)
        chunk = payload[cursor:chunk_end]
        remaining = total_len - chunk_end
        if 0 < remaining <= 2:
            take = min(max_payload - len(chunk), remaining)
            if take > 0:
                chunk = payload[cursor:chunk_end + take]
                chunk_end += take
                remaining = total_len - chunk_end
        has_more = remaining > 0
        if has_more:
            wrapped = struct.pack("<H", total_len - cursor) + chunk
        else:
            wrapped = chunk
        seq = _next_write_seq()
        packets.append(build_single_packet(cmd, seq, wrapped, encrypt, checksum, has_more, aes_key))
        cursor = chunk_end
    return packets

def build_single_packet(cmd, seq, payload, encrypt, checksum, frag, aes_key):
    if encrypt:
        if not aes_key:
            raise RuntimeError("Missing AES key for encrypted packet")
        payload_wire = aes_cfb_encrypt(aes_key, seq, payload)
    else:
        payload_wire = payload
    flags = 0
    if encrypt:
        flags |= 0x01
    if checksum:
        flags |= 0x02
    if frag:
        flags |= 0x10
    out = bytearray([cmd & 0xFF, flags & 0xFF, seq & 0xFF, len(payload_wire) & 0xFF])
    out.extend(payload_wire)
    if checksum:
        crc = crc16_app(0, bytes([seq & 0xFF, len(payload_wire) & 0xFF]))
        if payload:
            crc = crc16_app(crc, payload)
        out.extend(struct.pack("<H", crc))
    return bytes(out)

def ema_encrypt_json_hexascii(request_json):
    data = request_json.encode("utf-8")
    if len(data) % 16 != 0:
        data += b"\x00" * (16 - (len(data) % 16))
    key = pad_key_16(AES_KEY_STR)
    iv = AES_IV_STR.encode("utf-8")[:16]
    cipher = AES.new(key, AES.MODE_CBC, iv=iv)
    ciphertext = cipher.encrypt(data)
    return ciphertext.hex().encode("ascii")

def ema_decrypt_payload(payload):
    key = pad_key_16(AES_KEY_STR)
    iv = AES_IV_STR.encode("utf-8")[:16]
    cipher = AES.new(key, AES.MODE_CBC, iv=iv)
    decrypted = cipher.decrypt(payload)
    return decrypted.rstrip(b"\x00").decode("utf-8", errors="replace").strip()

class BlufiCodec:
    def __init__(self, mtu=BLUFI_MTU):
        self.mtu = mtu
        self.read_seq = -1
        self._rx_buf = bytearray()
        self._rx_hdr = None
    def parse_notify(self, raw, aes_key=None):
        if len(raw) < 4:
            return None
        type_subtype = raw[0]
        flags = raw[1]
        seq = raw[2]
        data_len = raw[3]
        encrypt = (flags & 0x01) != 0
        checksum = (flags & 0x02) != 0
        frag = (flags & 0x10) != 0
        need = 4 + data_len + (2 if checksum else 0)
        if len(raw) < need:
            return None
        payload_wire = raw[4:4+data_len]
        if encrypt:
            if not aes_key:
                raise RuntimeError("Encrypted notify received but AES key is not set")
            payload = aes_cfb_decrypt(aes_key, seq, payload_wire)
        else:
            payload = bytes(payload_wire)
        if checksum:
            got = raw[4+data_len] | (raw[4+data_len+1] << 8)
            crc = crc16_app(0, bytes([seq & 0xFF, data_len & 0xFF]))
            if payload:
                crc = crc16_app(crc, payload)
            if got != crc:
                raise RuntimeError(f"Checksum mismatch: got=0x{got:04x} expected=0x{crc:04x}")
        self.read_seq = (self.read_seq + 1) & 0xFF
        if seq != self.read_seq:
            self.read_seq = seq
        frame_type = type_subtype & 0x03
        subtype = (type_subtype >> 2) & 0x3F
        if frag:
            if len(payload) < 2:
                raise RuntimeError("Fragmented payload too short for total length header")
            frag_total = payload[0] | (payload[1] << 8)
            data = payload[2:]
            if self._rx_hdr is None:
                self._rx_hdr = (frame_type, subtype, flags)
                self._rx_buf.clear()
            self._rx_buf.extend(data)
            return None
        if self._rx_hdr is not None:
            self._rx_buf.extend(payload)
            data = bytes(self._rx_buf)
            frame_type, subtype, first_flags = self._rx_hdr
            self._rx_hdr = None
            self._rx_buf.clear()
            return (frame_type, subtype, first_flags, seq, data)
        return (frame_type, subtype, flags, seq, payload)

async def main():
    if len(sys.argv) < 2:
        print("Usage: python3 probe_ble_elt12_full.py <BLE_MAC>")
        sys.exit(1)
    address = sys.argv[1]
    async with BleakClient(address) as client:
        print("Connected, waiting 0.5s for device to settle...")
        await asyncio.sleep(0.5)
        # 1. Read device name
        print("Reading device name...")
        name_raw = await client.read_gatt_char(DEVICE_NAME_CHAR)
        device_name = bytes(name_raw).decode("utf-8", errors="ignore").strip("\x00\r\n ")
        print(f"Device name: {device_name}")
        if not device_name:
            print("Could not read device name")
            sys.exit(1)
        # 2. Blufi DH handshake
        print("Starting Blufi DH handshake...")
        p = int(BLUFI_DH_P_HEX, 16)
        g = BLUFI_DH_G
        priv = secrets.randbelow(p - 3) + 2
        pub = pow(g, priv, p)
        p_bytes = bytes.fromhex(BLUFI_DH_P_HEX)
        g_bytes = g.to_bytes(1, "big")
        pub_bytes = pub.to_bytes(128, "big")
        cmd_nego = make_cmd(1, 0)
        total_len = len(p_bytes) + len(g_bytes) + len(pub_bytes) + 6
        nego_payload_0 = bytes([0, (total_len >> 8) & 0xFF, total_len & 0xFF])
        nego_payload_1 = (
            bytes([1, 0, len(p_bytes)]) + p_bytes + bytes([0, len(g_bytes)]) + g_bytes + bytes([0, len(pub_bytes)]) + pub_bytes
        )
        codec = BlufiCodec()
        frames = []
        def on_notify(_sender, data):
            frame = codec.parse_notify(bytes(data))
            if frame:
                frames.append(frame)
        await client.start_notify(NOTIFY_CHAR, on_notify)
        await asyncio.sleep(0.1)
        print("Sending DH negotiation packets...")
        for pkt in build_packets(cmd_nego, nego_payload_0, False, False, None):
            await client.write_gatt_char(WRITE_CHAR, pkt, response=True)
            await asyncio.sleep(0.05)
        for pkt in build_packets(cmd_nego, nego_payload_1, False, False, None):
            await client.write_gatt_char(WRITE_CHAR, pkt, response=True)
            await asyncio.sleep(0.05)
        print("Waiting for DH response from device...")
        t0 = time.time()
        while time.time() - t0 < RESPONSE_TIMEOUT:
            if frames:
                break
            await asyncio.sleep(0.05)
        if not frames:
            print("No DH response from device")
            sys.exit(1)
        print("Received DH response from device.")
        dev_pub = int(frames[0][4].hex(), 16)
        shared = pow(dev_pub, priv, p)
        shared_hex = format(shared, "x")
        if len(shared_hex) % 2:
            shared_hex = "0" + shared_hex
        session_key = hashlib.md5(bytes.fromhex(shared_hex)).digest()
        # 3. Set security mode
        print("Setting security mode...")
        cmd_sec = make_cmd(0, 1)
        sec_packets = build_packets(cmd_sec, bytes([0x03]), False, True, session_key)
        for pkt in sec_packets:
            await client.write_gatt_char(WRITE_CHAR, pkt, response=True)
            await asyncio.sleep(0.05)
        # 4. Send encrypted local-data request
        print("Sending encrypted local-data request...")
        storage_id_raw = device_name.split("_")[-1]
        tried_any = False
        for storage_id in normalize_storage_ids(storage_id_raw):
            print(f"Trying storage_id: {storage_id}")
            request = {
                "company": "apsystems",
                "companyKey": "AmS4SV9oy3gk",
                "productKey": "PCS",
                "version": "1.0",
                "id": storage_id,
                "deviceId": storage_id,
                "type": "property",
                "eid": "2972245456",
                "method": "get",
                "identifier": "getDeviceLastDataLocal",
                "params": {
                    "T": "APS",
                    "V": "01",
                    "userId": "",
                    "EID": storage_id,
                    "systemId": "",
                    "storageId": storage_id,
                },
            }
            request_json = json.dumps(request, separators=(",", ":"))
            print(f"Request JSON for storage_id {storage_id}: {request_json}")
            payload = ema_encrypt_json_hexascii(request_json)
            print(f"Hex-encoded payload for storage_id {storage_id}: {payload.decode('ascii')}")
            cmd_custom = make_cmd(1, 19)
            packets = build_packets(cmd_custom, payload, True, True, session_key)
            frames.clear()
            print("Sending request packets...")
            for pkt in packets:
                await client.write_gatt_char(WRITE_CHAR, pkt, response=True)
                await asyncio.sleep(0.05)
            print("Waiting for response frame (and printing all notifications)...")
            t0 = time.time()
            seen = set()
            while time.time() - t0 < RESPONSE_TIMEOUT:
                for frame in frames:
                    # Print raw hex of every notification (once)
                    key = (frame[0], frame[1], frame[3], len(frame[4]), frame[4])
                    if key not in seen:
                        print(f"Notification: type={frame[0]} subtype={frame[1]} seq={frame[3]} len={len(frame[4])} raw={frame[4].hex()}")
                        seen.add(key)
                    if frame[0] == 1 and frame[1] == 19:
                        decrypted = ema_decrypt_payload(frame[4])
                        try:
                            parsed = json.loads(decrypted)
                            print(json.dumps(parsed, indent=2, ensure_ascii=False))
                        except Exception:
                            print("Decrypted payload (not JSON):", decrypted)
                        return
                await asyncio.sleep(0.05)
            tried_any = True
        if not tried_any:
            print("No storage IDs to try!")
        else:
            print("No response frame received for any storage ID variant.")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
