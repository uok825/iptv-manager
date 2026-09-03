import re
from dataclasses import dataclass, field


@dataclass
class M3UChannel:
    tvg_id: str = ""
    tvg_name: str = ""
    tvg_logo: str = ""
    group_title: str = ""
    display_name: str = ""
    url: str = ""
    extra_attrs: dict = field(default_factory=dict)


ATTR_PATTERN = re.compile(r'([\w-]+)="([^"]*)"')


def parse_m3u(content: str) -> list[M3UChannel]:
    channels = []
    lines = content.strip().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF:"):
            attrs = {}
            for key, val in ATTR_PATTERN.findall(line):
                attrs[key] = val

            comma_idx = line.rfind(",")
            display_name = line[comma_idx + 1:].strip() if comma_idx != -1 else ""

            url = ""
            i += 1
            while i < len(lines):
                next_line = lines[i].strip()
                if next_line and not next_line.startswith("#"):
                    url = next_line
                    break
                if next_line.startswith("#EXTINF:"):
                    i -= 1
                    break
                i += 1

            channels.append(M3UChannel(
                tvg_id=attrs.get("tvg-id", ""),
                tvg_name=attrs.get("tvg-name", ""),
                tvg_logo=attrs.get("tvg-logo", ""),
                group_title=attrs.get("group-title", ""),
                display_name=display_name,
                url=url,
                extra_attrs={k: v for k, v in attrs.items()
                             if k not in ("tvg-id", "tvg-name", "tvg-logo", "group-title")},
            ))
        i += 1
    return channels


def generate_m3u(channels: list[dict], epg_url: str = "") -> str:
    header = "#EXTM3U"
    if epg_url:
        header += f' url-tvg="{epg_url}"'
    lines = [header]
    for ch in channels:
        attrs = []
        epg_id = ch.get("epg_channel_id") or ch.get("tvg_id") or ""
        if epg_id:
            attrs.append(f'tvg-id="{epg_id}"')
        if ch.get("tvg_name"):
            attrs.append(f'tvg-name="{ch["tvg_name"]}"')
        if ch.get("tvg_logo"):
            attrs.append(f'tvg-logo="{ch["tvg_logo"]}"')
        if ch.get("group_name"):
            attrs.append(f'group-title="{ch["group_name"]}"')

        attr_str = " ".join(attrs)
        if attr_str:
            attr_str = " " + attr_str
        lines.append(f'#EXTINF:-1{attr_str},{ch.get("display_name", "")}')
        lines.append(ch.get("stream_url", ""))
    return "\n".join(lines) + "\n"
