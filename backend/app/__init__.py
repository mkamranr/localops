"""LocalOps backend."""

import os

# Set at image build time (Dockerfile ARG LOCALOPS_VERSION); "dev" for source checkouts.
__version__ = os.environ.get("LOCALOPS_VERSION") or "dev"
