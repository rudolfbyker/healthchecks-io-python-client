# healthchecks-io-python-client

A Python client for healthchecks.io with useful context managers.

None of the others I found online did what I needed at the time of writing:
- https://pypi.org/project/healthchecks-io/

## Examples

### Monitor a job with a UUID

Use a UUID when the check already exists in Healthchecks.io.

```python
import os

from hcio_client import HealthChecks


hc = HealthChecks()
backup_check = hc.check(uuid=os.environ["HCIO_BACKUP_UUID"])

with backup_check:
    run_backup()
```

The context manager sends a `/start` ping before the job runs. If the block
finishes normally, it sends a success ping. If the block raises an exception, it
sends the exception details to `/log`, sends a `/fail` ping, and then lets the
exception continue to propagate.

### Send pings manually

Use manual pings when the monitored work is not shaped like one `with` block.

```python
import os

from hcio_client import HealthChecks


hc = HealthChecks()
check = hc.check(uuid=os.environ["HCIO_IMPORT_UUID"])

check.ping_start()

exit_code = run_import()
check.ping_exit_code(code=exit_code)

if exit_code != 0:
    check.ping_log(data=f"Import failed with exit code {exit_code}")
```

You can also call `ping_success()` and `ping_failure()` directly.

### Use a slug and ping key

Use a ping key when you want stable, readable slugs instead of storing every
check UUID in your application config.

```python
import os

from hcio_client import HealthChecks


hc = HealthChecks(
    ping_key=os.environ["HCIO_PING_KEY"],
    create=True,
)

with hc.check(slug="nightly-backup"):
    run_backup()
```

With `create=True`, Healthchecks.io can auto-provision the check the first time
the slug is pinged.

### Create or update a check before running it

Pass a management API key when you want the client to create or update check
metadata before sending pings.

```python
import os

from hcio_client import HealthChecks


hc = HealthChecks(
    ping_key=os.environ["HCIO_PING_KEY"],
    manage_key=os.environ["HCIO_MANAGE_KEY"],
    create=True,
)

with hc.check(
    slug="nightly-backup",
    name="Nightly backup",
    desc="Backs up the application database and uploaded files.",
    timeout=60 * 60,
    grace=10 * 60,
):
    run_backup()
```

When `create=True` and check metadata is provided, the client calls the
management API first and upserts a check with `unique=("slug",)`. The returned
UUID is then used for the ping URLs.

### Update an existing check by UUID

```python
import os

from hcio_client import HealthChecks


hc = HealthChecks(manage_key=os.environ["HCIO_MANAGE_KEY"])

check = hc.check(
    uuid=os.environ["HCIO_REPORT_UUID"],
    name="Daily report",
    desc="Generates and emails the daily operations report.",
    timeout=30 * 60,
    grace=5 * 60,
)

with check:
    send_daily_report()
```

Providing management fields such as `name`, `desc`, `timeout`, or `grace`
updates the check before it is returned.

### Work with the management API

```python
import os

from hcio_client import HealthChecks


hc = HealthChecks(manage_key=os.environ["HCIO_MANAGE_KEY"])

backup_checks = hc.list(tags=["backup"])

check_info = hc.create_or_update(
    name="Weekly cleanup",
    slug="weekly-cleanup",
    tags="maintenance cleanup",
    timeout=2 * 60 * 60,
    grace=15 * 60,
    unique=("slug",),
)

cleanup_check = hc.check(uuid=check_info["uuid"])
cleanup_check.manage_update(desc="Deletes expired temporary files.")
```

### Make ping failures visible

Ping calls are best-effort by default. If you want ping request failures or HTTP
error responses to fail your program, opt in per call.

```python
import os

from hcio_client import HealthChecks


hc = HealthChecks(
    request_timeout=10.0,
    n_ping_attempts=5,
    n_wait_between_ping_attempts=1.0,
)

check = hc.check(uuid=os.environ["HCIO_BACKUP_UUID"])

check.ping_start(
    raise_for_failed_request=True,
    raise_for_status=True,
)
```
