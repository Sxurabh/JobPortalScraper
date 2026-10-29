from dataclasses import dataclass, field


@dataclass(frozen=True)
class Job:
    title: str
    url: str
    location: str = ""
    description: str = ""


@dataclass
class Company:
    name: str
    url: str
    keywords: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "Company":
        return cls(
            name=str(data["name"]).strip(),
            url=str(data["url"]).strip(),
            keywords=[str(value).strip() for value in data.get("keywords", []) if str(value).strip()],
            locations=[str(value).strip() for value in data.get("locations", []) if str(value).strip()],
            enabled=bool(data.get("enabled", True)),
        )

    def to_dict(self) -> dict:
        return {"name": self.name, "url": self.url, "keywords": self.keywords, "locations": self.locations, "enabled": self.enabled}

    def matches(self, job: Job) -> bool:
        text = f"{job.title}\n{job.location}\n{job.description}".casefold()
        if self.keywords and not any(term.casefold() in text for term in self.keywords):
            return False
        if self.locations and not any(place.casefold() in job.location.casefold() for place in self.locations):
            return False
        return True
