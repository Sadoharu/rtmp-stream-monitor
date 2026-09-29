# Architecture and diagnostic limits

## Observation chain

Each registered agent observes one stream URL and has a role, host/platform, location, and unique bearer token. It sends one-second samples through a five-second heartbeat batch. The API stores raw samples, updates a per-stream incident, and exposes current state and history to the authenticated dashboard.

```text
encoder-side SOURCE probe ── RTMP ingest/server ── SERVER_EGRESS_LOCAL ── TCP path ── CLIENT probes
                                                 └── SRS HTTP API ingress counters
```

`SERVER_EGRESS` connects to the RTMP server's loopback URL. The dashboard labels it `SERVER_EGRESS_LOCAL`. It answers “what does the server give a local reader?” It cannot prove exactly what arrived at the server. For SRS, `SERVER_INGRESS` polls the read-only `/api/v1/streams` HTTP API on loopback and observes publisher activity, receive bytes/bitrate, frame counters and codec metadata. It does not decode media or validate GOP/keyframe quality. An active SRS publisher therefore does not prove clean ingress frames: if egress is degraded, the classifier keeps the diagnosis unconfirmed. Keep the SRS HTTP API bound to loopback and leave its raw API disabled. A future decoded ingress adapter or a separate encoder-side `SOURCE` probe is needed for frame-level source/ingest diagnosis.

## Media analysis profiles

`DEEP` runs FFmpeg with null output and decodes each frame. `showinfo` supplies frame PTS, keyframe flag and I/P/B picture type; the frame analyzer counts I pictures separately and only advances the GOP on FFmpeg's keyframe flag. An I picture with `iskey:0` is visible as a non-key I frame and does not reset the keyframe-gap timer. This avoids treating every intra-coded picture as a decoder recovery point. The analyzer calculates observed GOP intervals, flags an initial keyframe absence at the configured threshold or five seconds by default, then adapts the threshold to 2.5 times the median observed GOP with a five-second floor. The filter chain also runs `freezedetect`, `silencedetect`, and audio `ashowinfo`. Known FFmpeg decode diagnostics are parsed from stderr. No re-encoding occurs.

The dashboard keeps the input's reported frame rate (`source_fps`) separate from FFmpeg's processing throughput (`decode_fps`). FFmpeg can decode faster or slower than real time, so its progress `fps` value must not be presented as the stream's frame rate.

`LIGHT` runs `ffprobe -show_packets` and reads key packet flags, packet sizes, PTS/DTS, packet arrival and GOP interval without decoding. It cannot identify every codec-specific IDR distinction, detect corrupt decoded pictures, or run freeze/silence filters. In both profiles, an I-frame/key packet is not guaranteed to be an independently decodable IDR for every codec/container.

When FFmpeg reconnects, the agent resets per-connection PTS/DTS and GOP continuity baselines so a source that restarts timestamps at zero does not create a false regression across separate RTMP sessions. Lifetime frame, keyframe, decode-error and reconnect counters remain cumulative.

Deep decode CPU cost depends on codec, resolution, frame rate, and hardware. The agent reports its FFmpeg process CPU and RSS; validate before enabling deep probes at scale. A dedicated second ffprobe process is not run alongside deep mode, avoiding a second full stream reader.

## Correlation and incident confidence

The backend compares the latest sample from each probe over a 20-second wall-clock window, then uses media PTS spread as a second alignment check when multiple probes report PTS. The default tolerance is five seconds and is configurable. Matching source/ingress errors point to `SOURCE_OR_INGEST_PROBLEM`; a clean, media-validated ingress plus broken local egress points to `RTMP_SERVER_RESTREAM_PROBLEM`. SRS publisher counters without decoded-media validation are insufficient for that diagnosis and keep it explicitly unconfirmed. Healthy local egress and a broken client point to `NETWORK_PATH_PROBLEM` when transport counters support it, otherwise `CLIENT_PROBLEM`. A media PTS lag can also support a low-confidence network-path diagnosis only when both compared probes use `LIGHT` packet inspection and their sample receive times differ by no more than the configured media tolerance. Deep-mode PTS can lag because decoding is behind, so it is not treated as transport proof. PTS lag describes media-timeline delay; it does not measure packet loss or identify a specific network device. A clean encoder-side `SOURCE` probe does not prove server ingress.

The PTS lag is directional: positive means the client is behind server egress. A client PTS ahead of server egress is a timestamp mismatch and does not independently support a network-path diagnosis.

Incident evaluation uses telemetry observation time. Delayed samples replayed from an agent outbox remain available in raw telemetry, but cannot resolve or replace an incident that began later; a long-running active diagnosis stays one incident until the probes report recovery or a newer diagnosis.

RTMP over TCP does not expose a frame identity shared by independent decoders. Wall-clock offset, buffering, retransmission and path asymmetry limit root-cause certainty. The dashboard therefore presents a probable location, symptoms, samples and diagnostic excerpts instead of claiming proof.

## Transport and time sources

- Linux uses `ss -ti` flow information when present and ICMP echo timing separately.
- Windows uses `Get-NetTCPStatistics` retransmit counter deltas (system-wide) and `Get-NetTCPConnection` state. Windows does not provide an equivalent per-flow retransmission count through this implementation, so a retransmit delta alone produces `NETWORK_PATH_UNCONFIRMED`; RTT/loss/state evidence or aligned packet-mode PTS lag is needed to attribute the issue to the client path.
- Windows uses structured .NET PingReply data; Linux runs ping in the C locale for stable parsing. Zero ICMP replies are reported as `NO_REPLY` with loss left unknown, because a target can block echo requests; one lost echo in a three-packet sample is not enough to classify a network fault. Substantial partial loss can support that diagnosis. No capture ring is enabled.
- Agent NTP state comes from the operating system. Offset reported from central HTTP Date is coarse (one-second date precision) and only approximate; it does not replace NTP.

## Durability and retention

Each agent first commits a compact JSON sample to a WAL-enabled, bounded SQLite outbox. Media, network, clock, and agent resource metrics are captured into the sample before it is queued, so replaying a backlog cannot rewrite the observation with newer measurements. A 401 is treated as a configuration fault and remains queued; transient network or server failures retry. The central API uses SQLAlchemy sessions with SQLite by default and is compatible with PostgreSQL URLs. Samples have stable IDs so retries do not create duplicates.

The central dashboard computes agent connectivity from receipt time and stream telemetry freshness from the sample's original observation time. A connected agent replaying an old outbox is therefore shown as `TELEMETRY_STALE` until a recent stream observation arrives.

Raw samples are retained for seven days. At hourly maintenance, complete older one-minute buckets are summarized per agent (numeric avg/min/max/last, worst status, and event counts) before raw rows are deleted. Aggregates are retained for 90 days; incidents for 180 days. Correlation stores up to 60 seconds of nearby samples and recent stderr excerpts per incident.

## Security boundaries

Dashboard/API administration uses a generated bearer token saved in a local token file; each agent has a separately stored SHA-256 token hash and can only submit for its registered stream. The first dashboard token is not exposed through an unauthenticated endpoint. Keep the central API on a trusted network or behind TLS/reverse proxy and restrict TCP/8090 to probe and management addresses. YAML contains agent bearer tokens and must be protected at rest.
