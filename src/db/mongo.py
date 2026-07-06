import os
from motor.motor_asyncio import AsyncIOMotorClient

class MongoDBClient:
    def __init__(self):
        self.client = None
        self.db = None
        
    async def connect(self):
        mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
        self.client = AsyncIOMotorClient(mongo_uri)
        self.db = self.client.get_database("kv_simulator")
        print(f"Connected to MongoDB at {mongo_uri}")
        
    async def disconnect(self):
        if self.client:
            self.client.close()
            print("Disconnected from MongoDB")
            
    def get_requests_collection(self):
        return self.db.get_collection("requests")
        
    def get_events_collection(self):
        return self.db.get_collection("events")
        
    def get_block_ledger_collection(self):
        return self.db.get_collection("block_ledger")

db_client = MongoDBClient()
