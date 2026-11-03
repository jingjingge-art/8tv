# 8TV playlist

This repository scans public GitHub playlist files for 8TV entries, checks candidate stream URLs, and writes responsive results to `8tv.txt`.

## Automation

GitHub Actions runs the scan every six hours. You can also start it manually from the Actions tab using **Scan 8TV playlist → Run workflow**.

## Local run

Requires Python 3.10+ and a GitHub token that can access code search:

```bash
python -m pip install -r requirements.txt
GH_SEARCH_TOKEN=your_token python scan_8tv.py


直播源搜索引擎

http://tonkiang.us


http://www.foodieguide.com/iptvsearch/
