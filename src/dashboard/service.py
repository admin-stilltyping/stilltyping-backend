import random
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo

from context_agent.schemas import DomainError

from .catalog import USE_CASES, WIDGET_TYPES, catalog_for, infer_use_case
from .demo import widget_data
from .models import DashboardConfig
from .schemas import ConfigOutput, DashboardOutput, WidgetOutput


def choose_widgets(seed: int, previous=()) -> list[str]:
    rng = random.Random(seed)
    selected = []
    # Balance concise metrics, visual charts and operational detail.
    for group, count in [("summary", 2), ("chart", 6), ("detail", 2)]:
        selected.extend(
            rng.sample([widget.id for widget in WIDGET_TYPES if widget.group == group], count)
        )
    if set(selected) == set(previous):
        replacement = next(
            widget.id
            for widget in WIDGET_TYPES
            if widget.group == "chart" and widget.id not in selected
        )
        selected[2] = replacement
    return selected


async def get_config(session, business):
    config = await session.get(DashboardConfig, business.id)
    if config is None:
        seed = secrets.randbelow(2**31)
        config = DashboardConfig(
            business_id=business.id,
            use_case=infer_use_case(business.name, business.description),
            widget_ids=choose_widgets(seed),
            demo_seed=seed,
            as_of=datetime.now(ZoneInfo(business.timezone)).date(),
        )
        session.add(config)
        await session.flush()
    return config


def check_revision(config, expected):
    if config.revision != expected:
        raise DomainError(
            409,
            "dashboard_changed",
            "This dashboard changed in another tab. Reload it before saving.",
        )


def render_dashboard(business, config, period):
    catalog = catalog_for(config.use_case)
    definitions = {item["id"]: item for item in catalog}
    return DashboardOutput(
        business_id=business.id,
        business_slug=business.slug,
        business_name=business.name,
        period=period,
        config=ConfigOutput(
            use_case=config.use_case,
            widget_ids=config.widget_ids,
            revision=config.revision,
            as_of=config.as_of,
        ),
        use_cases=USE_CASES,
        catalog=catalog,
        widgets=[
            WidgetOutput(
                **definitions[key],
                data=widget_data(key, config.use_case, config.demo_seed, config.as_of, period),
            )
            for key in config.widget_ids
        ],
    )
