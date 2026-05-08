## Server Configuration
- Production: app.example.com (SSH port 22)
- Staging: stage.example.com
- Deploy command: `./deploy.sh --env <stage|prod>`

## Database
- PostgreSQL 16 on db.example.com:5432
- Connection pool: 20 max connections
- Backup schedule: daily at 03:00 UTC

## Monitoring
- Grafana: monitoring.example.com:3000
- Alert channel: #alerts on Slack
- Log retention: 30 days
