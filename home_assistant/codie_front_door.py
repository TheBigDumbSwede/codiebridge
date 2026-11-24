"""Send a front-door announcement to the Codie bridge on this host."""

import json
import sys
from uuid import uuid4

from paho.mqtt import publish


MESSAGES = {
    "ring": "Someone rang the front doorbell.",
    "person": "Someone is at the front door.",
}


def main():
    message = MESSAGES[sys.argv[1]]
    publish.single(
        "codie/bridge/announce",
        payload=json.dumps({"id": str(uuid4()), "text": message}),
        qos=1,
        retain=False,
        hostname="127.0.0.1",
    )


if __name__ == "__main__":
    main()
