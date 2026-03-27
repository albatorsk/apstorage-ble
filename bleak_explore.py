import asyncio
from bleak import BleakClient, BleakScanner

async def explore():
    # Or connect by MAC directly
    async with BleakClient("AA:BB:CC:DD:EE:FF") as client:
        print(f"Connected: {client.is_connected}")
        for service in client.services:
            print(f"\nService: {service.uuid}")
            for char in service.characteristics:
                print(f"  Char: {char.uuid} | props: {char.properties}")
                if "read" in char.properties:
                    try:
                        val = await client.read_gatt_char(char.uuid)
                        print(f"    Value: {val.hex()} ({list(val)})")
                    except Exception as e:
                        print(f"    Read error: {e}")

asyncio.run(explore())
