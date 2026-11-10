# OSPF Failover Lab

Two leaves, two spines and two test hosts. The lab measures what happens when one leaf uplink goes down.

Requires Python 3.10 or later. Run `python3 topology.py` and open http://127.0.0.1:18454 to replay the saved packet captures. Live mode and the measurement script require Docker Compose 2.36 or later.

```sh
docker compose up -d --build
python3 measure.py
docker compose down
```

New measurements go under `runs/`. For Live lab mode, keep the containers running and select **Send 6 pings**.

[Report](DODC%20Assignment%202%20OSPF%20Failover.docx) · [Recorded results](evidence/results.json)
