uv run --env-file .env uvicorn app.main:create_app --factory --port 8000

http://127.0.0.1

- Store: http://store.localhost (the 3 seeded products are on the shop page)
- Admin: http://store.localhost/wp-admin
  - Username: admin
  - Password: 6nLllf9JjU

claude mcp add --transport http -s user team-gateway http://prod.gw.localhost/mcp \
--header "Authorization: Bearer mcpg_2ob0_ppRyxnyuHyf5SDwDwfymaNiUAjN1qdrCCoEW5s"
