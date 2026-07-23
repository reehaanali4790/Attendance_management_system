import argparse
import sys
import time
from zk_service import ZKService

def main():
    parser = argparse.ArgumentParser(description="ZKTeco Physical Device Smoke Test Utility")
    parser.add_argument("--ip", default="192.168.18.58", help="IP address of the physical ZK device")
    parser.add_argument("--port", type=int, default=4370, help="Port of the ZK device (typically 4370)")
    parser.add_argument("--key", type=int, default=0, help="Communication key / password (default 0)")
    args = parser.parse_args()

    print(f"=== Starting ZKTeco Device Smoke Test ===")
    print(f"Target IP:   {args.ip}")
    print(f"Target Port: {args.port}")
    print(f"Comm Key:    {args.key}")
    print("-----------------------------------------")

    service = ZKService(ip=args.ip, port=args.port, comm_key=args.key)
    
    try:
        print("1. Attempting connection for User Fetch...")
        service.connect()
        print("SUCCESS: Connected to device for users!")
        
        print("\n2. Fetching users...")
        users = service.get_users()
        print(f"SUCCESS: Retrieved {len(users)} users.")
        if users:
            print("First 5 users:")
            for u in users[:5]:
                uid = getattr(u, 'uid', 'N/A')
                user_id = getattr(u, 'user_id', 'N/A')
                name = getattr(u, 'name', 'N/A')
                priv = getattr(u, 'privilege', 'N/A')
                card = getattr(u, 'card', '')
                print(f"  - UID: {uid}, User ID: {user_id}, Name: {name}, Privilege: {priv}, Card: {card}")
                
        print("\nDisconnecting User Fetch Session...")
        service.disconnect()
        time.sleep(2)  # Give the device 2 seconds to rest the socket
        
        print("\n3. Attempting connection for Attendance Fetch...")
        service.connect()
        print("SUCCESS: Connected to device for logs!")
        
        print("\nFetching attendance logs...")
        logs = service.get_attendance()
        print(f"SUCCESS: Retrieved {len(logs)} logs.")
        if logs:
            print("Most recent 5 logs:")
            sorted_logs = sorted(logs, key=lambda x: getattr(x, 'timestamp', datetime.datetime.min), reverse=True)
            for l in sorted_logs[:5]:
                uid = getattr(l, 'user_id', 'N/A')
                ts = getattr(l, 'timestamp', 'N/A')
                status = getattr(l, 'status', 'N/A')
                punch = getattr(l, 'punch', 'N/A')
                print(f"  - User ID: {uid}, Time: {ts}, Status: {status}, Punch: {punch}")
                
    except Exception as e:
        print(f"\nERROR: Failed during smoke test execution.")
        print(f"Detail: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        print("\n4. Final Disconnect...")
        try:
            service.disconnect()
            print("SUCCESS: Cleanly disconnected from device.")
        except Exception as e:
            print(f"Warning during disconnect: {e}")

    print("\n=== Smoke Test Completed Successfully ===")

if __name__ == "__main__":
    import datetime
    main()
