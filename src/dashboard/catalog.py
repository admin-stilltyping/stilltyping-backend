from dataclasses import dataclass

USE_CASES = {
    "support": "Customer support",
    "clinic": "Clinic & healthcare",
    "retail": "Retail & commerce",
    "services": "Professional services",
}


@dataclass(frozen=True)
class WidgetDefinition:
    id: str
    label: str
    group: str
    description: str


WIDGET_TYPES = (
    WidgetDefinition("kpi", "KPI card", "summary", "One key number and its change over time"),
    WidgetDefinition("progress", "Progress bar", "summary", "Progress toward a business goal"),
    WidgetDefinition("gauge", "Gauge", "summary", "A percentage against a clear target"),
    WidgetDefinition("sparkline", "Sparkline", "summary", "A headline value with a compact trend"),
    WidgetDefinition("line", "Line chart", "chart", "How a measure changes over time"),
    WidgetDefinition("area", "Area chart", "chart", "The volume of activity over time"),
    WidgetDefinition("horizontal_bar", "Horizontal bar", "chart", "Compare named categories"),
    WidgetDefinition("vertical_bar", "Vertical bar", "chart", "Compare activity across periods"),
    WidgetDefinition("grouped_bar", "Grouped bar", "chart", "Compare two measures side by side"),
    WidgetDefinition("stacked_bar", "Stacked bar", "chart", "See how parts contribute to a total"),
    WidgetDefinition("pie", "Pie chart", "chart", "Compare the parts of a whole"),
    WidgetDefinition("donut", "Donut chart", "chart", "A distribution with a total at its centre"),
    WidgetDefinition("funnel", "Funnel", "detail", "Follow a journey from start to finish"),
    WidgetDefinition("heatmap", "Heatmap", "detail", "Spot the busiest days and times"),
    WidgetDefinition("table", "Ranked table", "detail", "See leading categories and exact values"),
    WidgetDefinition("activity", "Activity list", "detail", "Review recent business activity"),
)
TYPE_BY_ID = {widget.id: widget for widget in WIDGET_TYPES}

# Each use case supplies business language while all charts share the same renderer contract.
PROFILES = {
    "clinic": {
        "titles": [
            "Appointments",
            "Booking goal",
            "Patient satisfaction",
            "Returning patients",
            "Bookings over time",
            "Patient visits",
            "Popular treatments",
            "Appointments by period",
            "New & returning patients",
            "Booking channels",
            "Visit types",
            "Appointment status",
            "Patient booking journey",
            "Busy appointment hours",
            "Leading treatments",
            "Recent clinic activity",
        ],
        "categories": ["Consultation", "Dental care", "Follow-up", "Health check"],
        "segments": ["New visit", "Follow-up", "Check-up", "Specialist"],
        "statuses": ["Completed", "Confirmed", "Pending", "Cancelled"],
        "series": ["New patients", "Returning patients"],
        "channels": ["Online", "Phone"],
        "stages": ["Enquiries", "Bookings", "Confirmed", "Attended"],
        "events": [
            "Demo appointment confirmed",
            "Demo follow-up scheduled",
            "Demo visit completed",
            "Demo enquiry received",
        ],
        "unit": "appointments",
        "target": 600,
    },
    "retail": {
        "titles": [
            "Orders received",
            "Order goal",
            "Fulfilment rate",
            "Repeat customers",
            "Orders over time",
            "Items sold",
            "Popular categories",
            "Orders by period",
            "New & repeat buyers",
            "Sales channels",
            "Category mix",
            "Order status",
            "Shopping journey",
            "Busy shopping hours",
            "Leading categories",
            "Recent shop activity",
        ],
        "categories": ["Home", "Accessories", "Clothing", "Electronics"],
        "segments": ["Home", "Accessories", "Clothing", "Electronics"],
        "statuses": ["Delivered", "Processing", "Shipped", "Returned"],
        "series": ["New buyers", "Repeat buyers"],
        "channels": ["Web store", "Messaging"],
        "stages": ["Visits", "Product views", "Checkouts", "Purchases"],
        "events": [
            "Demo order received",
            "Demo parcel dispatched",
            "Demo delivery completed",
            "Demo return reviewed",
        ],
        "unit": "orders",
        "target": 1200,
    },
    "services": {
        "titles": [
            "Service bookings",
            "Project goal",
            "On-time completion",
            "Returning clients",
            "Enquiries over time",
            "Work completed",
            "Popular services",
            "Bookings by period",
            "New & returning clients",
            "Enquiry channels",
            "Service mix",
            "Project status",
            "Client booking journey",
            "Busy enquiry hours",
            "Leading services",
            "Recent service activity",
        ],
        "categories": ["Consulting", "Design", "Maintenance", "Training"],
        "segments": ["Consulting", "Design", "Maintenance", "Training"],
        "statuses": ["Completed", "In progress", "Scheduled", "On hold"],
        "series": ["New clients", "Returning clients"],
        "channels": ["Website", "Referrals"],
        "stages": ["Enquiries", "Proposals", "Accepted", "Completed"],
        "events": [
            "Demo enquiry received",
            "Demo proposal accepted",
            "Demo project completed",
            "Demo session scheduled",
        ],
        "unit": "bookings",
        "target": 400,
    },
    "support": {
        "titles": [
            "Support conversations",
            "Resolution goal",
            "Customer satisfaction",
            "Returning customers",
            "Conversations over time",
            "Messages received",
            "Common topics",
            "Tickets by period",
            "New & returning customers",
            "Contact channels",
            "Conversation topics",
            "Ticket status",
            "Support journey",
            "Busy support hours",
            "Leading support topics",
            "Recent support activity",
        ],
        "categories": ["Account help", "Order questions", "Billing", "General advice"],
        "segments": ["Account help", "Orders", "Billing", "Other"],
        "statuses": ["Resolved", "In progress", "Open", "Waiting"],
        "series": ["New customers", "Returning customers"],
        "channels": ["Web chat", "Messaging"],
        "stages": ["Conversations", "Questions identified", "Answers provided", "Resolved"],
        "events": [
            "Demo conversation opened",
            "Demo question answered",
            "Demo ticket resolved",
            "Demo feedback received",
        ],
        "unit": "conversations",
        "target": 1000,
    },
}


def catalog_for(use_case: str) -> list[dict]:
    profile = PROFILES[use_case]
    return [
        {
            "id": widget.id,
            "type": widget.id,
            "type_label": widget.label,
            "group": widget.group,
            "title": title,
            "description": widget.description,
        }
        for widget, title in zip(WIDGET_TYPES, profile["titles"], strict=True)
    ]


def infer_use_case(name: str, description: str | None) -> str:
    import re

    words = set(re.findall(r"[a-z]+", f"{name} {description or ''}".lower()))
    for use_case, terms in (
        ("clinic", {"clinic", "dental", "doctor", "hospital", "healthcare", "patient"}),
        ("retail", {"shop", "store", "retail", "commerce", "reseller", "shopping"}),
        ("services", {"consulting", "agency", "services", "consultancy", "salon"}),
    ):
        if words & terms:
            return use_case
    return "support"
