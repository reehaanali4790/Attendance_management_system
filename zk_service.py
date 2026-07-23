import datetime
from typing import List, Any
import logging

try:
    from zk import ZK, const
except ImportError:
    # Fallback to prevent crash if not installed
    class DummyConst:
        STATUS_CHECKIN = 1
        STATUS_CHECKOUT = 0
    const = DummyConst()

logger = logging.getLogger("ZKService")
logging.basicConfig(level=logging.INFO)

class ZKService:
    def __init__(self, ip: str, port: int, comm_key: int = 0):
        self.ip = ip
        self.port = port
        self.comm_key = comm_key
        self.zk = None
        self.conn = None
        
    def connect(self):
        logger.info(f"Connecting to ZKTeco device at {self.ip}:{self.port} (comm_key: {self.comm_key})...")
        try:
            self.zk = ZK(self.ip, port=self.port, timeout=90, password=self.comm_key, force_udp=False)
            self.conn = self.zk.connect()
            self.conn.disable_device()
            logger.info("Connected to physical ZKTeco device.")
            return self
        except Exception as e:
            logger.error(f"Failed to connect to device: {e}")
            raise e
            
    def disconnect(self):
        if self.conn:
            try:
                self.conn.enable_device()
                self.conn.disconnect()
                logger.info("Disconnected physical ZKTeco device.")
            except Exception as e:
                logger.error(f"Error disconnecting: {e}")
        self.conn = None
        self.zk = None

    def get_users(self) -> List[Any]:
        if not self.conn:
            raise Exception("Device not connected")
        return self.conn.get_users()

    def get_attendance(self) -> List[Any]:
        if not self.conn:
            raise Exception("Device not connected")
        return self.conn.get_attendance()
