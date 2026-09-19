"""Movie mode: drive a hue-ghost PC client (https://github.com/engabd11/HueGhost).

hue-ghost keeps a muted "ghost" copy of whatever a TV's Jellyfin client is
playing in lockstep on a PC, where the official Hue Sync desktop app captures
it and streams to the entertainment area — a software Sync Box. The
integration's only job here is to switch it on and off from Home Assistant
(and hand the entertainment area over cleanly, since a bridge allows one
streamer per area), plus expose its state and a couple of live knobs.
"""
