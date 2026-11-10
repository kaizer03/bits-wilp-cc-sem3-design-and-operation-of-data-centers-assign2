# OSPF Failover Lab

Two leaves, two spines and two test hosts. The experiment removes one leaf uplink while pinging across the fabric.

Requires Docker Compose 2.36 or later and Python 3.10 or later.

```sh
docker compose up -d --build
python3 measure.py
docker compose down
```

The runner waits for both routes, runs three trials and restores the link after each one. New output goes under `runs/`.

[Report](DODC%20Assignment%202%20OSPF%20Failover.docx) · [Recorded results](evidence/results.json)
