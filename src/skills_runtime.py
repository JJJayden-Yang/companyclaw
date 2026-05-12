import re
from pathlib import Path

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


class SkillStore:
    def __init__(self, skills_dir: Path) -> None:
        self.skills_dir = skills_dir
        self.skills: dict[str, dict[str, str | dict]] = {}
        self.reload()

    def reload(self) -> int:
        self.skills = {}
        if not self.skills_dir.exists():
            return 0
        for path in sorted(self.skills_dir.rglob("SKILL.md")):
            text = path.read_text(encoding="utf-8")
            meta, body = self._parse_frontmatter(text)
            name = str(meta.get("name", path.parent.name)).strip() or path.parent.name
            self.skills[name] = {"meta": meta, "body": body, "path": str(path)}
        return len(self.skills)

    def _parse_frontmatter(self, text: str) -> tuple[dict, str]:
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text.strip()
        raw_meta = match.group(1)
        body = match.group(2).strip()
        if HAS_YAML:
            try:
                meta = yaml.safe_load(raw_meta) or {}
                if isinstance(meta, dict):
                    return meta, body
            except Exception:
                pass
        meta: dict[str, str] = {}
        for line in raw_meta.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip().strip("'\"")
            if key:
                meta[key] = value
        return meta, body

    def descriptions(self) -> str:
        if not self.skills:
            return "(no skills available)"
        lines: list[str] = []
        for name, item in self.skills.items():
            meta = item.get("meta", {})
            desc = ""
            if isinstance(meta, dict):
                desc = str(meta.get("description", "")).strip()
            if not desc:
                desc = "No description"
            lines.append(f"- {name}: {desc}")
        return "\n".join(lines)

    def list_names(self) -> list[str]:
        return sorted(self.skills.keys())

    def load(self, name: str) -> str:
        skill = self.skills.get(name)
        if not skill:
            names = ", ".join(self.list_names()) or "(none)"
            return f"Error: Unknown skill '{name}'. Available: {names}"
        body = str(skill.get("body", ""))
        return f"<skill name=\"{name}\">\n{body}\n</skill>"


def build_system_prompt(base_prompt: str, store: SkillStore) -> str:
    return (
        f"{base_prompt}\n\n"
        "You can use skills for specialized knowledge.\n"
        "If a task needs domain-specific process, call `load_skill` first.\n"
        "Available skills:\n"
        f"{store.descriptions()}"
    )
