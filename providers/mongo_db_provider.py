from pymongo import MongoClient, ReturnDocument


class MongoDB:
    def __init__(self, connection_string, db_name):
        self.client = MongoClient(connection_string)
        self.db = self.client[db_name]

    def create(self, collection_name, data):
        collection = self.db[collection_name]
        result = collection.insert_one(data)
        print("CREATE ->", result.inserted_id)

    def load_last_task(self, collection_name):
        collection = self.db[collection_name]
        last_task = collection.find_one_and_update(
            filter={"status": "pending"},
            sort=[("created_at", -1)],
            update={"$set": {"status": "working"}},
            return_document=ReturnDocument.AFTER,
        )
        if last_task:
            print("New task received")
            return last_task
        else:
            return
