# oracle-snatch

OCIのAlways Free A1.Flexインスタンスを、無料枠と既存リソースを確認しながら安全に取得するツールです。

- [日本語ドキュメント](README.ja.md)
- [English documentation](README.en.md)

This tool requests one OCI Always Free A1.Flex instance at a time, checks the tenancy-wide free allowance, and keeps durable idempotency state to prevent duplicate creation after a restart.
