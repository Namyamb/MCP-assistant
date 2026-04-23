import sys
import logging

# Suppress googleapiclient info messages
logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)
logging.getLogger('googleapiclient').setLevel(logging.WARNING)

# Enable DEBUG for orchestrator so we can trace LLM responses
logging.basicConfig(level=logging.DEBUG,
                    format='%(name)s %(levelname)s %(message)s',
                    handlers=[logging.StreamHandler()])
logging.getLogger('app.core.orchestrator').setLevel(logging.DEBUG)

from app.main import main

if __name__ == "__main__":
    sys.exit(main())

