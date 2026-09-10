#!/usr/bin/env bash
cd "$(dirname "$0")"

echo "======================================================="
echo "  Starting Copita"
echo "======================================================="

# Run the local service on port 8888 and open the web interface
python3 -m copita.cli serve --port 8888 --open
