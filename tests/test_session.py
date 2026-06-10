from harness.events import SessionStarted, UserMessage
from harness.log import read_session
from harness.session import Session, SubscriberBus
from harness.types import SessionId


def test_append_stamps_monotonic_seq(tmp_path):
    with Session(tmp_path, SessionId("s1")) as session:
        session.start()
        e1 = session.append(UserMessage(text="one"))
        e2 = session.append(UserMessage(text="two"))
    assert (e1.seq, e2.seq) == (2, 3)  # start() consumed seq 1
    envs = read_session(tmp_path, SessionId("s1"))
    assert isinstance(envs[0].event, SessionStarted)
    assert [e.seq for e in envs] == [1, 2, 3]


def test_subscribers_receive_envelopes(tmp_path):
    with Session(tmp_path, SessionId("s1")) as session:
        queue = session.bus.subscribe()
        session.start()
        session.append(UserMessage(text="hi"))
        received = [queue.get_nowait() for _ in range(queue.qsize())]
    assert [type(e.event).__name__ for e in received] == ["SessionStarted", "UserMessage"]


def test_full_subscriber_queue_drops_oldest_not_session():
    bus = SubscriberBus()
    queue = bus.subscribe(maxsize=2)

    class FakeEnv:
        def __init__(self, n):
            self.n = n

    for n in range(5):
        bus.publish(FakeEnv(n))  # must never raise or block
    drained = [queue.get_nowait().n for _ in range(queue.qsize())]
    assert drained == [3, 4]  # oldest dropped


def test_session_blob_store_lives_in_sidecar_dir(tmp_path):
    with Session(tmp_path, SessionId("s1")) as session:
        ref = session.blobs.put(b"payload")
    assert (tmp_path / "sessions" / "s1" / "blobs" / ref.sha256).exists()


def test_parent_linkage_recorded(tmp_path):
    with Session(tmp_path, SessionId("child"), parent=(SessionId("parent"), 7)) as session:
        session.start()
    envs = read_session(tmp_path, SessionId("child"))
    assert envs[0].event.parent_session_id == "parent"
    assert envs[0].event.parent_seq == 7


def test_start_twice_raises(tmp_path):
    import pytest
    with Session(tmp_path, SessionId("s1")) as session:
        session.start()
        with pytest.raises(RuntimeError, match="already called"):
            session.start()
