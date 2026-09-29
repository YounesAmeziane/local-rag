# session.py
# Per-conversation state shared by chat.py's CLI loop and webui.py. One Session
# per conversation -- exactly the fields chat.ask() reads/returns each turn.

class Session:
    def __init__(self):
        self.reset()

    def reset(self):
        self.history: list[dict] = []
        self.topic_table = None
        self.last_sql = None
        self.last_intent = None
        self.last_route = None
