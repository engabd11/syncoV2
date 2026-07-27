"""Backend-agnostic music library + playback layer for the Synco media player.

Exactly one :class:`~.base.LibraryBackend` is active at a time — Music Assistant,
or a direct OpenSubsonic/Navidrome server — chosen in the integration options and
switchable at runtime. The dashboard card and the ``media_player`` entity talk
only to this layer, which keeps the backends interchangeable.
"""
