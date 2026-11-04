#!/bin/sh
# Keep 90 days of raw GBFS snapshots on the 700GB disk.
find /data/gbfs -type f -name "*.json.gz" -mtime +90 -delete
