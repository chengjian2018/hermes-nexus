import threading

from dialogue.base import DialogueContext


class Session:
    def __init__(self, session_id, pattern_code):
        self.session_id = session_id
        self.pattern_code = pattern_code
        # Pattern object for the current turn (injected at launch; may be a per-request custom pattern)
        self.pattern = None
        # Outbound task info (injected at launch)
        self.task_info = None
        self.history = []
        self.status = None
        self.usr_msg = None
        self.silence_cnt = 0
        self.update = None

        # Serializes chat turns on this session: concurrent requests (channel
        # redelivery, client double-send) must not interleave one turn's
        # begin_turn reset / history writes / end_turn append with another's.
        # Lives on the object so eviction drops it together with the session.
        self.turn_lock = threading.Lock()

        # DB generation of this session object, captured by the store at
        # create_session / restore time. The store uses it to reject writes
        # from an in-flight turn of an evicted-and-relaunched generation
        # (cross-generation pollution guard); None until first persisted.
        self.launch_epoch = None

        # Dialogue pipeline context: flows through pattern.stages, carrying history, slots, recall results, etc.
        # cxt.user_query is updated before each turn; session state is written back from cxt at turn end
        self.cxt = DialogueContext(session_id=session_id, user_query="")


