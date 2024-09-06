"""Stream type classes for tap-klaviyo."""

from __future__ import annotations

import typing as t
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from singer_sdk.helpers.jsonpath import extract_jsonpath

from tap_klaviyo.client import KlaviyoStream

if t.TYPE_CHECKING:
    from urllib.parse import ParseResult

SCHEMAS_DIR = Path(__file__).parent / Path("./schemas")


class MissingConfigException(Exception):
    pass


class EventsStream(KlaviyoStream):
    """Define custom stream."""

    name = "events"
    path = "/events"
    primary_keys = ["id"]
    replication_key = "datetime"
    schema_filepath = SCHEMAS_DIR / "event.json"

    def post_process(
        self,
        row: dict,
        context: dict | None = None,  # noqa: ARG002
    ) -> dict | None:
        row["datetime"] = row["attributes"]["datetime"]
        return row

    @property
    def is_sorted(self) -> bool:
        return True


class CampaignsStream(KlaviyoStream):
    """Define custom stream."""

    name = "campaigns"
    path = "/campaigns"
    primary_keys = ["id"]
    replication_key = None
    schema_filepath = SCHEMAS_DIR / "campaigns.json"
    included_jsonpath = "$[included][*]"
    included_map = {}

    @property
    def partitions(self) -> list[dict] | None:
        return [
            {
                "filter": "equals(messages.channel,'email')",
            },
            {
                "filter": "equals(messages.channel,'sms')",
            },
        ]

    def get_url_params(
        self,
        context: dict | None,
        next_page_token: ParseResult | None,
    ) -> dict[str, t.Any]:
        url_params = super().get_url_params(context, next_page_token)

        # Apply channel filters
        if context:
            url_params["filter"] = context["filter"]
        url_params["include"] = "tags,campaign-messages"

        return url_params

    def post_process(
        self,
        row: dict,
        context: dict | None = None,  # noqa: ARG002
    ) -> dict | None:
        row["updated_at"] = row["attributes"]["updated_at"]
        row["tags"] = [self.included_map[tag["id"]]
                       for tag in row.get("relationships", {}).get("tags", {}).get("data", [])]
        row["campaign_messages"] = [self.included_map[campaign_message["id"]]
                                    for campaign_message in
                                    row.get("relationships", {}).get("campaign-messages", {}).get("data", [])]
        return row

    def get_child_context(self, record: dict, context: t.Optional[dict]) -> dict:
        """Return a context dictionary for child streams."""
        return {
            "campaign_id": record["id"],
            "campaign_status": record["attributes"]["status"],
            "send_time": record["attributes"]["send_time"],
        }

    def parse_response(self, response: requests.Response) -> t.Iterable[dict]:
        self.process_included(response)
        yield from super().parse_response(response)

    def process_included(self, response: requests.Response):
        self.included_map = {included['id']: included for included in
                             extract_jsonpath(self.included_jsonpath, input=response.json())}


class CampaignValuesReportsStream(KlaviyoStream):
    name = "campaign_values_reports"
    path = "/campaign-values-reports"
    rest_method = "POST"
    primary_keys = ["id"]
    replication_key = None
    parent_stream_type = CampaignsStream
    schema_filepath = SCHEMAS_DIR / "campaign_values_reports.json"
    backoff_max_tries = 10
    state_partitioning_keys = []
    ignore_parent_replication_key = True

    def post_process(self, row: dict, context: dict | None = None) -> dict | None:
        row["campaign_id"] = context["campaign_id"]
        row["campaign_status"] = context["campaign_status"]
        row["generated_at"] = datetime.now().isoformat()
        return row

    def prepare_request_payload(
            self,
            context: dict | None,
            next_page_token: t.Optional[t.Any],
    ) -> dict | None:
        return {
            "data": {
                "type": "campaign-values-report",
                "attributes": {
                    "statistics": [
                        "click_rate",
                        "click_to_open_rate",
                        "clicks",
                        "clicks_unique",
                        "delivered",
                        "delivery_rate",
                        "open_rate",
                        "opens",
                        "opens_unique",
                        "recipients",
                        "unsubscribe_rate",
                        "unsubscribe_uniques",
                        "unsubscribes",
                        "bounced",
                        "bounce_rate"
                        ],
                    "timeframe": {
                        "key": "last_12_months"
                    },
                    "conversion_metric_id": "WcGvVS",
                    "filter": f"equals(campaign_id,\"{context['campaign_id']}\")"
                }
            }
        }

    def backoff_wait_generator(self) -> t.Generator[float, None, None]:
        def _backoff_from_headers(retriable_api_error):
            response_headers = retriable_api_error.response.headers
            return int(response_headers.get("Retry-After", 60))

        return self.backoff_runtime(value=_backoff_from_headers)

    def get_records(self, context: dict | None) -> t.Iterable[dict[str, t.Any]]:
        if not self.config.get("report_campaigns_sent_last_n_days"):
            raise MissingConfigException('report_campaigns_sent_last_n_days config is required '
                                         'to limit the reports requests.')
        # Only fetch records if the campaign has been sent
        if (context['campaign_status'] in ('Sent', 'Variations Sent')
                and context.get('send_time')
                # Campaigns sent in the last n days
                and datetime.fromisoformat(context['send_time']) >=
                datetime.now(timezone.utc) - timedelta(days=self.config['report_campaigns_sent_last_n_days'])):
            self.logger.info(f"Fetching campaign values report for campaign {context['campaign_id']}"
                             f" sent at {context['send_time']}")
            yield from super().get_records(context)
        yield from []  # Return an empty list if no campaign is not sent in the period


class ProfilesStream(KlaviyoStream):
    """Define custom stream."""

    name = "profiles"
    path = "/profiles"
    primary_keys = ["id"]
    replication_key = "updated"
    schema_filepath = SCHEMAS_DIR / "profiles.json"
    max_page_size = 100

    def post_process(
        self,
        row: dict,
        context: dict | None = None,  # noqa: ARG002
    ) -> dict | None:
        row["updated"] = row["attributes"]["updated"]
        return row

    @property
    def is_sorted(self) -> bool:
        return True


class MetricsStream(KlaviyoStream):
    """Define custom stream."""

    name = "metrics"
    path = "/metrics"
    primary_keys = ["id"]
    replication_key = None
    schema_filepath = SCHEMAS_DIR / "metrics.json"

    def post_process(
        self,
        row: dict,
        context: dict | None = None,  # noqa: ARG002
    ) -> dict | None:
        row["updated"] = row["attributes"]["updated"]
        return row


class ListsStream(KlaviyoStream):
    """Define custom stream."""

    name = "lists"
    path = "/lists"
    primary_keys = ["id"]
    replication_key = "updated"
    schema_filepath = SCHEMAS_DIR / "lists.json"

    def get_child_context(self, record: dict, context: dict | None) -> dict:
        context = context or {}
        context["list_id"] = record["id"]

        return super().get_child_context(record, context)  # type: ignore[no-any-return]

    def post_process(
        self,
        row: dict,
        context: dict | None = None,  # noqa: ARG002
    ) -> dict | None:
        row["updated"] = row["attributes"]["updated"]
        return row


class ListPersonStream(KlaviyoStream):
    """Define custom stream."""

    name = "listperson"
    path = "/lists/{list_id}/relationships/profiles/"
    primary_keys = ["id"]
    # the parent stream’s replication key won’t get updated when child items are changed
    ignore_parent_replication_key = True
    replication_key = None
    parent_stream_type = ListsStream
    schema_filepath = SCHEMAS_DIR / "listperson.json"
    max_page_size = 1000

    def post_process(self, row: dict, context: dict) -> dict | None:
        row["list_id"] = context["list_id"]
        return row

    def get_url_params(
            self,
            context: dict | None,
            next_page_token: ParseResult | None,
    ) -> dict[str, t.Any]:
        params = super().get_url_params(context, next_page_token)
        # The filter improves the performance of the API call
        params["filter"] = f"greater-or-equal(joined_group_at,1900-01-01T00:00:00+00:00)"
        params["sort"] = "joined_group_at"
        return params


class ListPersonIncrementalStream(KlaviyoStream):
    """Incremental implementation of ListPersonStream.
    This stream is used to fetch incremental data from the ListPersonStream.
    Note: This stream doesn't detect removed records, to get the removed records you
    should consider using the EventsStream to get the Unsubscribed events."""

    name = "listperson-incremental"
    path = "/lists/{list_id}/relationships/profiles/"
    primary_keys = ["id"]
    ignore_parent_replication_key = True
    replication_key = 'joined_group_at'
    parent_stream_type = ListsStream
    schema_filepath = SCHEMAS_DIR / "listperson_incremental.json"
    max_page_size = 1000
    filter_compare = "greater-or-equal"

    def __init__(self, tap, name=None, schema=None, path=None):
        super().__init__(tap, name, schema, path)
        self.sync_started = datetime.now(timezone.utc)

    def post_process(self, row: dict, context: dict) -> dict | None:
        row["list_id"] = context["list_id"]
        row["joined_group_at"] = self.sync_started
        return row



class FlowsStream(KlaviyoStream):
    """Define custom stream."""

    name = "flows"
    path = "/flows"
    primary_keys = ["id"]
    replication_key = "updated"
    schema_filepath = SCHEMAS_DIR / "flows.json"

    def post_process(
        self,
        row: dict,
        context: dict | None = None,  # noqa: ARG002
    ) -> dict | None:
        row["updated"] = row["attributes"]["updated"]
        return row


class TemplatesStream(KlaviyoStream):
    """Define custom stream."""

    name = "templates"
    path = "/templates"
    primary_keys = ["id"]
    replication_key = "updated"
    schema_filepath = SCHEMAS_DIR / "templates.json"

    def post_process(
        self,
        row: dict,
        context: dict | None = None,  # noqa: ARG002
    ) -> dict | None:
        row["updated"] = row["attributes"]["updated"]
        return row

    @property
    def is_sorted(self) -> bool:
        return True


class SegmentsStream(KlaviyoStream):
    name = "segments"
    path = "/segments"
    primary_keys = ["id"]
    replication_key = "updated"
    schema_filepath = SCHEMAS_DIR / "segments.json"

    def post_process(
        self,
        row: dict,
        context: dict | None = None,  # noqa: ARG002
    ) -> dict | None:
        row["updated"] = row["attributes"]["updated"]
        return row

    @property
    def is_sorted(self) -> bool:
        return True
