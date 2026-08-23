from __future__ import annotations

import os

import alibabacloud_oss_v2 as oss

from .production import build_app_from_environment


app = build_app_from_environment(os.environ, sdk=oss)
