"""Publishing to YouTube: metadata, thumbnails, tcap parity, OAuth, upload.

The package is deliberately separate from the edit pipeline. Its input is a
rendered ``exports`` row (a frozen MP4), so publishing never re-renders a clip
and never mutates the export it reads.
"""

from __future__ import annotations
