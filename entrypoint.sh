#!/bin/sh
# Write environment variables to a file that cron can source.
# (cron runs with a bare environment, so MAILGUN_/SMTP_/EMAIL_/INKIND_ vars must
#  be exported into the cron job explicitly.)
env | grep -E '^(MAILGUN_|SMTP_|EMAIL_|DATA_DIR|INKIND_|TZ)' > /app/env.sh
sed -i 's/^/export /' /app/env.sh

# Schedule (cron syntax, container local time). Override via env if desired.
#   INKIND_CRON_DAILY  : change-check run (default: 09:00 every day except Mon)
#   INKIND_CRON_WEEKLY : full weekly digest (default: 09:00 Monday)
CRON_DAILY="${INKIND_CRON_DAILY:-0 9 * * 0,2-6}"
CRON_WEEKLY="${INKIND_CRON_WEEKLY:-0 9 * * 1}"

# Source env first so the mail credentials are present in each cron job.
printf '%s\n%s\n' \
  "$CRON_DAILY . /app/env.sh && python3 /app/inkind_monitor.py >> /proc/1/fd/1 2>&1" \
  "$CRON_WEEKLY . /app/env.sh && python3 /app/inkind_monitor.py --weekly >> /proc/1/fd/1 2>&1" \
  | crontab -

# Run once on startup (background so crond starts immediately). First run sets
# the baseline snapshot and emails it.
python3 /app/inkind_monitor.py &

# Start cron in the foreground.
exec crond -f -l 2
