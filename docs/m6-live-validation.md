# RTMP Monitor 2.0 — live validation notes (M6)

## Ubuntu 22.04 runtime smoke — 2026-09-29

An isolated Ubuntu 22.04 container ran the project with system Python 3.10.12 and FFmpeg 4.4.2. The full Python test suite passed: `113 passed, 1 skipped` (one Starlette/httpx deprecation warning).

For a controlled fault scenario, an FFmpeg 720p50 publisher sent a stream through a local SRS 6 container while concurrent Ubuntu `LIGHT` and `DEEP` probes reported to a temporary SQLite collector. Both profiles returned measured series (`21` and `20` one-second buckets). `LIGHT` measured a floor of `0.068 Mbps` and peak of `7.479 Mbps`; `DEEP` measured `0 bps` and `7.327 Mbps`. `DEEP` recorded `FREEZE_START` at 0 bps with last frame/audio age `1.094 s`, then `FREEZE_END` after bitrate recovered to `7.327 Mbps`. The publisher remained connected during the controlled black-picture interval. This is a decode/freeze fixture, not an injected network fault.

A separate 40-second read-only run used two concurrent Ubuntu probes against the user-provided live RTMP source. The series endpoint returned `28` measured `LIGHT` buckets and `26` measured `DEEP` buckets; missing seconds were represented as `NO_SAMPLE`, not zero. The mean/minimum/maximum were `8.443/2.760/15.139 Mbps` (`LIGHT`) and `8.236/0.449/18.443 Mbps` (`DEEP`). No events occurred in that selected interval. FFmpeg CPU/RSS in that run was `1.1% / 50.2 MB` (`LIGHT`) and `26.2% / 177.0 MB` (`DEEP`).

In a separate 20-second two-probe capture, the shared Docker `eth0` counters changed by `64.80 MiB RX` and `3.69 MiB TX`, averages of `21.23 Mbit/s RX` and `1.21 Mbit/s TX`. This is a combined container-interface delta including both probes and transport overhead; it is not per-agent network use or production capacity evidence.

All Ubuntu runs were Docker Desktop containers on the Windows development PC, not the user's physical Ubuntu server. They verify this Jammy/Python/FFmpeg runtime path but do not verify target-host performance, independent network locations, or a cause for the observed `NO_SAMPLE` gaps. Temporary containers and network are removed after validation.

## Read-only multi-probe smoke — 2026-09-29

Three sequential 20-second runs used the user-provided live RTMP source. Each run launched two independent `CLIENT` agents concurrently on one Windows host: one `LIGHT`, one `DEEP`. Each run used a temporary SQLite central collector bound to `127.0.0.1`; the database, tokens, and agent queues were removed when the process exited. Both agents connected to the same source, and the V2 series endpoint returned measured 1-second buckets for both probes in every run.

| Run | LIGHT latest / series mean | DEEP latest / series mean | Measured buckets (LIGHT / DEEP) | Event rows |
|---|---:|---:|---:|---:|
| 1 | 3.428 / 8.275 Mbps | 3.428 / 8.347 Mbps | 18 / 18 | 2; first script version did not print event details |
| 2 | 6.318 / 8.218 Mbps | 6.471 / 8.213 Mbps | 18 / 18 | 0 |
| 3 | 7.926 / 8.665 Mbps | 8.278 / 8.135 Mbps | 18 / 17 | 2 |

“Latest” is the probe's last rolling-window sample; “series mean” is the average of returned one-second buckets across the full run. These values vary because the feed and its scene complexity change over time; the difference is not by itself evidence of packet loss or network throughput.

Run 3 captured a real `PTS_REGRESSION` on the `DEEP` client at `2026-09-29T13:53:35.078904Z`: media PTS moved from `2.24017 s` back to `1.907 s`. At the event, last video/audio frame ages were `0.016 s` and `0.032 s`. The correlation engine produced a `CLIENT_PATH_UNCONFIRMED` warning with `UNCONFIRMED` confidence. The explanation stated that source/server/client causes cannot be separated because `SERVER_INGRESS` and `SERVER_EGRESS` observations were absent. This is the correct evidentiary limit; the isolated PTS event does not establish a root cause or a sustained playback fault.

The multi-probe smoke is reproducible with:

```powershell
py -3.12 scripts/live-multiprobe-smoke.py "rtmp://<host>:1935/live/<stream>" --seconds 20 --profiles LIGHT DEEP
```

It reports per-profile status, measured bitrate, series bucket count, FFmpeg CPU/RSS, and event codes/evidence. It does not store the test URL in the repository. The agents share a host and network, so this validates concurrent ingest and same-stream series/API behavior, not independent network locations.

After adding CPU/RSS and concise event output, a 12-second script verification returned 9 measured buckets per profile and no events. FFmpeg usage on this host was `LIGHT` 1.6% CPU / 20.9 MB RSS and `DEEP` 26.1% CPU / 178.5 MB RSS. These are one short sample on the development PC, not capacity guarantees.

## Controlled RTMP bitrate dip and freeze — 2026-09-29

An isolated SRS 6 RTMP server ran in a temporary localhost-only Docker container on port `19350`. The reusable [publisher fixture](../scripts/publish-rtmp-fault-fixture.ps1) sent a 720p50 H.264/AAC stream and rendered the video black for six seconds while keeping the RTMP publish session active. Two Windows `CLIENT` probes (`LIGHT` and `DEEP`) and a temporary SQLite central collector ran concurrently for 35 seconds; the source and collector were removed after the smoke.

Both profiles returned measured 1-second API buckets (32 each). Their minimum bucket was `0.159 Mbps`, compared with peaks of `4.635 Mbps` (`LIGHT`) and `4.743 Mbps` (`DEEP`), ratios `0.034` and `0.033`; the normal picture returned to about `4.4 Mbps`. `DEEP` recorded `FREEZE_START` at `2026-09-29T15:05:32.388653Z`, then `FREEZE_DURATION`/`FREEZE_END` at `2026-09-29T15:05:36.407810Z`. The returned freeze event included the low measured bitrate and frame/audio ages; the end marker showed recovered bitrate. This confirms a controlled encoded-media dip and decode-side freeze reach the same V2 series/events API. It does not inject a real packet-loss condition or test independent network sites.

On this development PC, the same run reported FFmpeg process CPU/RSS of `LIGHT` 0.0% / 20.9 MB and `DEEP` 22.7% / 104.9 MB. A preceding short run measured DEEP at 9.2% CPU, so these are workload-sensitive observations, not capacity limits. Reproduce with the two commands in [the bitrate spike report](v2-bitrate-spike.md).

## Controlled event in the production dashboard — 2026-09-29

The browser was pointed at the temporary collector created by `live-multiprobe-smoke.py --serve-after 240` after a longer (120-second) SRS publisher run. The fixture blackout was set to seconds 50–56 so it fell inside the 55-second capture; the earlier 45-second publisher/capture attempt had ended the source before capture finished and is not treated as a successful freeze test.

The API smoke assertions passed for both profiles. `LIGHT` had a `0.160 Mbps` floor, `4.751 Mbps` peak, and `0.034` floor/peak ratio; `DEEP` had `0.161 Mbps`, `4.952 Mbps`, and `0.033`. `DEEP` emitted `FREEZE_START` at `2026-09-29T16:09:42.253309Z`, followed by `FREEZE_DURATION` and `FREEZE_END` at `2026-09-29T16:09:46.231845Z`. The browser showed both measured lines, the dip, the shared event lane, and the corresponding event card. The card reported measured duration `6.02 s`, bitrate `4.95 Mbps`, last video/audio frame ages `0.047/0.062 s`, and zero decode errors. Its explanation said this was observed on the `CLIENT` probe and the fault location remained unconfirmed without upstream/egress probes. The `FREEZE_DURATION` event has a human label and exposes its measured seconds; completed point events without an interval no longer show elapsed wall-clock time as their duration. Selecting `15 хв` makes the short interval easier to inspect than the default one-hour range.

This verifies the controlled local Windows/SRS dashboard path from RTMP packets through agent, SQLite, V2 API, chart, and event details. The probes shared the same host/network; it does not establish independent-site localization or a root cause for live-feed problems.

The same local SRS run also produced `PTS_REGRESSION` warnings in `DEEP`. The smoke did not establish whether they came from RTMP timestamp handling or another decode-timeline effect, so they remain warnings without a root-cause claim. SRS also logged a small timestamp correction while the publisher was closing.

The reusable smoke accepts `--max-bitrate-floor-ratio` and repeatable `--require-event` assertions. The controlled run used `--max-bitrate-floor-ratio 0.5 --require-event FREEZE_START --require-event FREEZE_END`; all assertions passed.

## Isolated RTMP server restart — 2026-09-29

The new [server-restart smoke](../scripts/live-server-restart-smoke.py) started SRS `6.0.184` with its RTMP port published only on loopback, a temporary SQLite collector, and concurrent `LIGHT` and `DEEP` client probes. A local FFmpeg publisher supplied a 640x360 H.264/AAC test stream. Restarting SRS interrupted both readers; each probe recorded one FFmpeg reconnect and resumed measured telemetry without restarting the central collector or agent.

The passing run covered `2026-09-29T18:40:24Z` through `18:40:33Z`. Each profile had four measured one-second buckets after the restart. The last recovered averages were `1.072840 Mbps` (`LIGHT`) and `1.073352 Mbps` (`DEEP`). Both latest rolling metrics were `MEASURED` at the end of the run, and the shared event timeline contained `FFMPEG_RESTART`. `PTS_REGRESSION`, `AV_TIMESTAMP_DRIFT`, and `CLIENT_PATH_UNCONFIRMED` were also present; those additional signals were not attributed to the SRS restart, and same-host probes still cannot localize an independent network cause.

A second run used a deliberate 10-second publisher GOP with a configured 2-second warning threshold before restarting SRS at `2026-09-29T18:45:16Z`. The event timeline contained both `KEYFRAME_GAP` and `KEYFRAME_GAP_END`; after the server restart both profiles reconnected and had three measured buckets. Their last recovered averages were `1.047184 Mbps` (`LIGHT`) and `1.052320 Mbps` (`DEEP`).

A repeat run at `2026-09-29T18:59:00Z` again produced both GOP markers, one `FFMPEG_RESTART` per profile, and four measured one-second buckets per profile after restart. The last averages were `1.077232 Mbps` (`LIGHT`) and `1.076904 Mbps` (`DEEP`). The script printed the correlated `CLIENT_PATH_UNCONFIRMED` events with `UNCONFIRMED` confidence and the text that client, server-restream, and upstream causes cannot be separated without `SERVER_EGRESS`; it also asserts that these diagnoses never claim higher confidence. These runs verify configured GOP-gap detection and recovery, not corrupted H.264 packets or a decode-error root cause. The script removes the temporary container and collector on exit.

Reproduce the combined long-GOP and server-restart scenario with:

```powershell
py -3.12 scripts/live-server-restart-smoke.py --publisher-gop-seconds 10 --keyframe-gap-threshold 2 --before-restart 15 --after-restart 25
```

## Deterministic H.264 corruption and decoder recovery — 2026-09-29

The [server-restart smoke](../scripts/live-server-restart-smoke.py) now accepts `--damage-video-frames FIRST LAST`. It uses FFmpeg's `noise` bitstream filter to corrupt a deterministic range of encoded video packets in the local publisher while leaving the RTMP session up; frames after the selected interval are clean. This is encoded-media corruption, not network packet loss.

Three independent runs with frames `50..70` (about `2.0..2.8 s` at 25 fps) each recorded `DECODE_ERROR` from the `DEEP` probe. Before restarting SRS, the decoder was still running and had resumed producing recent frames: `frames=379`, `last_frame_age=0.125 s` in the first run and `frames=379`, `last_frame_age=0.110 s` after the profile-classification fix. After the SRS restart both profiles reconnected and produced positive `MEASURED` bitrate buckets (`4–5` buckets per profile, depending on capture timing). The repeated command is:

```powershell
py -3.12 scripts/live-server-restart-smoke.py --damage-video-frames 50 70 --before-restart 12 --after-restart 12
```

The test exposed a labeling error: `LIGHT` uses `ffprobe` and does not decode frames, but its parser diagnostic had been counted as `DECODE_ERROR`. `LIGHT` now reports `BITSTREAM_PARSE_ERROR` at warning severity, while `DEEP` retains `DECODE_ERROR`; the API and dashboard explain the distinction. The post-fix integration run showed one `DECODE_ERROR` from `restart-deep`, one `BITSTREAM_PARSE_ERROR` from `restart-light`, recent decoded frames before the SRS restart, and recovered measured series after it. Other timestamp and unconfirmed-path markers appeared during the generated scenario; they do not establish a separate root cause.

This verifies reproducible bitstream damage, a decode diagnostic, and frame/bitrate recovery through SRS, agents, storage, and the V2 API on one Windows host. It does not verify actual network packet loss or independent network sites.

## What remains unverified for M6

- A simultaneous real `SERVER_EGRESS` probe and at least two clients on separate Windows/Ubuntu hosts are not available from this workstation. The three-probe localization chain cannot be accepted from same-host client probes.
- Controlled video-freeze, configured long-GOP/keyframe-gap, RTMP server-restart, client-only TCP outage, deterministic H.264 bitstream corruption/recovery, and injected RTMP-path IP packet loss/recovery scenarios were induced against isolated SRS sources. The freeze reached the browser; the other scenarios reached the V2 API with recovered measurements. The network-loss harness confirmed actual drops with `tc`, but the product did not diagnose them; independent-site localization and a production per-flow packet-loss signal remain unverified.
- The multi-probe API was exercised against a real feed. Browser rendering and evidence-card interaction were verified for the controlled local fixture; browser graph interaction for the live user feed remains unchecked. Earlier four-series load views were checked separately as noted in M2.
- No PostgreSQL deployment or migration of the user's Ubuntu systemd database was performed. Clean Windows/Ubuntu installs, real upgrade/rollback, and self-contained Windows/Ubuntu packages are still open under M4/M5.

These gaps require access to the target Ubuntu host and a separate Windows/Ubuntu client (or an isolated RTMP test server for controlled fault injection). They do not invalidate the verified concurrent measurement/API path, but M6 is not complete.

## Live RTMP API reread — 2026-09-29

A 30-second read-only Windows run launched concurrent `LIGHT` and `DEEP` client probes against the user-provided live RTMP source. Both agents reported `OK` and `MEASURED`; the shared V2 series endpoint returned 27 `LIGHT` and 26 `DEEP` one-second measured buckets. Series means were `8.255 Mbps` and `8.793 Mbps`; min/max were `2.151/14.201 Mbps` and `4.797/19.106 Mbps`. No events occurred in this interval. The initial missing/warmup seconds remained `NO_SAMPLE` / `MEASUREMENT_WARMUP`. FFmpeg usage on this host was `0% / 21.7 MB` for `LIGHT` and `35.9% / 186.7 MB` for `DEEP`.

This confirms the current live source still reaches the same V2 API from both profiles. Both probes share one Windows host and network, and the interval had no event; this is not an independent-site diagnosis or a live multi-probe browser check.

## Client-only TCP outage with a healthy server egress — 2026-09-29

The new [client-outage smoke](../scripts/live-client-outage-smoke.py) started an isolated SRS source and a temporary SQLite collector. The publisher and a `SERVER_EGRESS` probe connected directly to SRS; concurrent `LIGHT` and `DEEP` clients connected through a loopback TCP proxy. The script shut down only the proxy listener for eight seconds, so client FFmpeg and per-flow TCP checks lost connectivity while the publisher and server-egress path stayed active.

In the passing run, the outage lasted from `2026-09-29T20:00:12.203Z` to `20:00:20.196Z`. `SERVER_EGRESS` returned eight positive measured one-second buckets during that interval, averaging `0.994 Mbps`. Both clients recorded `FFMPEG_RESTART`, reached two reconnects, and returned to positive measured bitrate (`LIGHT` 0.699 → 0.636 Mbps; `DEEP` 0.699 → 1.058 Mbps). The event timeline included `NETWORK_PATH_PROBLEM` with probable location `NETWORK BETWEEN SERVER EGRESS AND AFFECTED CLIENT`, plus `FFMPEG_EXIT`/`FFMPEG_RESTART` for the client probes. A later `CLIENT_PROBLEM`/timestamp event appeared during recovery and is not attributed to the injected outage. This demonstrates that the local correlation path uses healthy egress and failed client TCP evidence to separate the client-facing network path from the source/server output.

Reproduce with:

```powershell
py -3.12 scripts/live-client-outage-smoke.py --outage-after 10 --outage-duration 8 --recovery-timeout 35
```

This is a controlled loopback connection outage on one Windows host. It does not verify packet corruption/loss, an ISP path, or independent physical observation sites. It also does not turn the test's injected cause into evidence available in a production incident unless probes observe the corresponding server-egress and client TCP states.

## Injected RTMP-path IP packet loss — 2026-09-30

The same [client-outage smoke](../scripts/live-client-outage-smoke.py) now supports `--packet-loss-percent`. It builds a small Linux relay container with `tc` and applies `tc netem loss` to the relay's egress interface during the fault window. The publisher and `SERVER_EGRESS` continue connecting directly to SRS; only the two client RTMP sessions pass through the lossy relay. The harness reads `tc`'s qdisc counters, so the injected loss is confirmed at the network layer rather than inferred from media corruption or a bitrate dip.

Two runs used 20% loss for 8 seconds. The relay reported 299 and 270 dropped packets. In both runs, direct `SERVER_EGRESS` stayed healthy and returned eight positive measured buckets (mean `1.037 Mbps` and `0.997 Mbps`). Both Windows clients stayed TCP-connected (zero reconnects) and submitted fresh measured telemetry after loss was removed. No Docker container or network from the smoke remained after cleanup.

Three stronger runs used 50% loss for 8 seconds. The relay reported 187, 118, and 108 dropped packets, while direct `SERVER_EGRESS` returned 9, 8, and 8 positive measured buckets with means of `0.997 Mbps`, `1.012 Mbps`, and `1.059 Mbps`. All three runs emitted `NETWORK_PATH_PROBLEM` for both client profiles while the fault was active, with media PTS lag of `11.251 s`, `10.581 s`, and `6.691 s`; clients recovered measured telemetry without TCP reconnects. The events remained `UNCONFIRMED`, which reflects that the app had evidence of client-side media lag and healthy egress but not proof of the injected packet-loss mechanism or a physical link. The smoke can require this diagnosis with `--require-event NETWORK_PATH_PROBLEM`.

After tightening the diagnosis to require contemporaneous `LIGHT` samples, a new 50% loss run recorded 117 qdisc drops, eight positive `SERVER_EGRESS` buckets averaging `0.997 Mbps`, and a `12.581 s` PTS lag. It still emitted the required `NETWORK_PATH_PROBLEM` while the fault was active; both clients produced fresh measured telemetry after recovery without reconnects. This confirms that the stricter sample-alignment gate still detects the severe controlled case. The deterministic incident explanation now reports the measured PTS difference at low confidence and explicitly says it does not prove packet loss or identify a network device.

At 20% loss, the product did not emit an explicit network-path diagnosis: the timeline only contained `PTS_REGRESSION` / `AV_TIMESTAMP_DRIFT` around recovery. Those events are not attributed to the injected loss. The test therefore confirms the network-layer fault and recovery at both rates, but the product only localized the severe 50% scenario through correlated media PTS lag. It did not measure or report the actual packet-loss rate. Windows host-wide TCP counters still cannot associate retransmits with a particular RTMP flow; independent client/server observation sites and a production-grade per-flow signal remain necessary for precise localization.

Reproduce with:

```powershell
py -3.12 scripts/live-client-outage-smoke.py --packet-loss-percent 50 --outage-after 6 --outage-duration 8 --recovery-timeout 35 --require-event NETWORK_PATH_PROBLEM
```
