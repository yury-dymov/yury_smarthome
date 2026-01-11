from datetime import datetime, timezone
from typing import Any
import threading
import time
import copy
import json
import requests
from posixpath import join as urljoin


class QPLPoint:
    timestamp: datetime
    name: str
    payload: dict[str, Any]

    def __init__(self, nm: str, pd: dict[str, Any] = {}):
        self.timestamp = datetime.now(timezone.utc)
        self.name = nm
        self.payload = copy.deepcopy(pd)

    def annotate(self, key: str, value: Any):
        self.payload[key] = value


class QPLPointEncoder(json.JSONEncoder):
    def encode(self, o):
        return {
            "name": o.name,
            "timestamp": o.timestamp.astimezone().isoformat(),
            "payload": o.payload,
        }


class QPLFlow:
    name: str
    payload: dict[str, Any]
    points: list[QPLPoint]
    outcome: str = ""
    start: datetime
    ended: datetime | None = None

    def __init__(self, nm: str):
        self.name = nm
        self.points = []
        self.payload = {}
        self.opened_subspans = []
        self.start = datetime.now(timezone.utc)

    def mark_point(self, nm: str, payload: dict[str, Any] = {}) -> QPLPoint | None:
        if self.outcome == "":
            point = QPLPoint(nm, payload)
            self.points.append(point)
            return point
        return None

    def mark_subspan_begin(self, nm: str) -> QPLPoint | None:
        if self.outcome == "":
            self.opened_subspans.append(nm)
            return self.mark_point(nm + "_begin")
        return None

    def mark_subspan_end(self, nm: str) -> QPLPoint | None:
        if self.outcome == "":
            if len(self.opened_subspans) > 0:
                last_elem = self.opened_subspans[-1]
                if last_elem == nm:
                    del self.opened_subspans[-1]
                    return self.mark_point(nm + "_end")
                else:
                    raise QPLAttemptedToEndSubspanBeforeEndingChildren
            else:
                raise QPLAttemptedToEndAlreadyEndedSubspan
        return None

    def annotate(self, key: str, value: Any):
        self.payload[key] = value

    def mark_success(self):
        if self.outcome == "":
            for subspan in list(reversed(self.opened_subspans)):
                self.mark_subspan_end(subspan)
            self.ended = datetime.now(timezone.utc)
            self.outcome = "SUCCESS"

    def mark_failed(self, error: str):
        if self.outcome == "":
            self.ended = datetime.now(timezone.utc)
            self.outcome = "FAILED"
            self.annotate("error", error)

    def mark_canceled(self):
        if self.outcome == "":
            self.ended = datetime.now(timezone.utc)
            self.outcome = "CANCELED"


class QPLFlowEncoder(json.JSONEncoder):
    def encode(self, o):
        return {
            "name": o.name,
            "points": list(map(lambda p: QPLPointEncoder().encode(p), o.points)),
            "annotations": o.payload,
            "outcome": o.outcome,
            "started": o.start.astimezone().isoformat(),
            "ended": o.ended.astimezone().isoformat() if o.ended else "-1",
        }


class QPLService:
    queue: list[QPLFlow] = []
    endpoint: str

    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        thread = threading.Thread(target=self.run, args=())
        thread.daemon = True
        thread.start()

    def run(self):
        while True:
            self._upload_queue_if_needed()
            time.sleep(10)

    def add_flow_to_upload_queue(self, flow: QPLFlow):
        self.queue.append(flow)

    def _upload_queue_if_needed(self):
        if len(self.queue) == 0:
            return
        copy_queue = copy.deepcopy(self.queue)

        flows = list(map(lambda x: QPLFlowEncoder().encode(x), copy_queue))
        r = requests.post(
            urljoin(self.endpoint, "qpl"),
            data=json.dumps({"flows": flows}),
            timeout=5,
        )

        if r.status_code == 201:
            self.queue = []


class QPL:
    service: QPLService

    def __init__(self):
        self.service = QPLService("http://zeus.loc:8124")

    def create_flow(self, name: str) -> QPLFlow:
        return QPLFlow(name)

    def add_completed_flow_to_upload_queue(self, flow: QPLFlow):
        self.service.add_flow_to_upload_queue(flow)


class QPLAttemptedToEndAlreadyEndedSubspan(Exception):
    """QPLAttemptedToEndAlreadyEndedSubspan."""


class QPLAttemptedToEndSubspanBeforeEndingChildren(Exception):
    """QPLAttemptedToEndSubspanBeforeEndingChildren."""
