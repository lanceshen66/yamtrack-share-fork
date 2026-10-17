"""临时脚本：用真实数据试渲染分享图，验证分栏与横屏自适应。"""

import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.contrib.auth import get_user_model
from django.utils import timezone

from app import share
from app.models import BasicMedia
from users.models import MediaStatusChoices

OUT = Path(__file__).resolve().parent.parent / ".runtime" / "share-test"
OUT.mkdir(parents=True, exist_ok=True)

User = get_user_model()
user = User.objects.first()
print("user:", user.username)

items = list(
    BasicMedia.objects.get_media_list(
        user=user,
        media_type="tv",
        status_filter=MediaStatusChoices.ALL.value,
        sort_filter="score",
    )
)
print("items:", len(items))
counts = {}
for item in items:
    counts[item.status] = counts.get(item.status, 0) + 1
print("status counts:", counts)

share.prefetch_posters(items)

cases = [
    ("landscape-all-grouped", dict(orientation="landscape", group_by_status=True)),
    ("portrait-all-grouped", dict(orientation="portrait", group_by_status=True)),
    ("landscape-all-flat", dict(orientation="landscape", group_by_status=False)),
    ("landscape-8", dict(orientation="landscape", group_by_status=False, limit=8)),
    ("landscape-3", dict(orientation="landscape", group_by_status=False, limit=3)),
]

for name, opts in cases:
    subset = items[: opts.get("limit")] if opts.get("limit") else items
    pages = share.build_share_pages(
        subset,
        media_type="tv",
        status=MediaStatusChoices.ALL.value,
        orientation=opts["orientation"],
        theme_name="dark",
        show_score=True,
        show_status=not opts["group_by_status"],
        group_by_status=opts["group_by_status"],
    )
    for index, page in enumerate(pages, start=1):
        path = OUT / f"{name}-{index}.png"
        page.save(path, "PNG", optimize=True)
        print(f"{name}: {page.width}x{page.height} -> {path.name} ({path.stat().st_size // 1024} KB)")

print("done", timezone.now())
