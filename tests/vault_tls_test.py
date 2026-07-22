"""VaultCA — корневой CA переиспользуется, leaf валиден и подписан корнем.

Проверяем фундамент TLS-перехвата (фаза 2, docs/ARCHITECTURE-claude-box.md §4.2):
корень не регенерится между запусками (иначе trust-store битый), issue(host) даёт
валидный leaf (цепочку доверия проверяем НАСТОЯЩИМ TLS-хендшейком — server c leaf +
client с нашим CA в trust), приватные ключи лежат 0600, leaf кэшируется по host.

Автономность (без orchestrator) держит соседний vault_domain_test через
walk_packages — vault.tls тянет только stdlib + openssl CLI.

Запуск: .venv/bin/python tests/vault_tls_test.py
"""
import socket
import ssl
import stat
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from vault.tls import LeafCert, VaultCA  # noqa: E402


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="vault_ca_"))


def test_ca_generated_and_reused():
    """Второй VaultCA на том же каталоге НЕ регенерит корень — тот же CA-серт."""
    d = _tmp()
    ca1 = VaultCA(d)
    pem1 = ca1.ca_cert_pem()
    key1 = ca1.ca_key.read_bytes()
    assert "BEGIN CERTIFICATE" in pem1
    # Новый объект на том же каталоге — байт-в-байт тот же корень (ключ и серт).
    ca2 = VaultCA(d)
    assert ca2.ca_cert_pem() == pem1, "корень регенерирован — trust-store инвалидируется"
    assert ca2.ca_key.read_bytes() == key1, "ключ CA перегенерён"
    print("OK VaultCA: корень генерируется и переиспользуется")


def test_ca_key_perms_0600():
    ca = VaultCA(_tmp())
    mode = stat.S_IMODE(ca.ca_key.stat().st_mode)
    assert mode == 0o600, f"ключ CA имеет права {oct(mode)}, ожидали 0600"
    print("OK VaultCA: ключ корня 0600")


def test_leaf_cached_by_host():
    """issue(host) дважды → тот же leaf (кэш), другой host → другие файлы."""
    ca = VaultCA(_tmp())
    a1 = ca.issue("api.example.com")
    a2 = ca.issue("api.example.com")
    assert a1 is a2, "leaf не закэширован по host"
    b = ca.issue("other.example.com")
    assert b.cert_path != a1.cert_path
    # Ключ leaf тоже 0600.
    assert stat.S_IMODE(a1.key_path.stat().st_mode) == 0o600
    print("OK VaultCA: leaf кэшируется по host, ключ leaf 0600")


def test_leaf_chain_valid_via_tls_handshake():
    """Живой TLS: server с leaf + client с нашим CA в trust → хендшейк проходит.

    Доказывает и подпись цепочки (leaf ← CA), и совпадение SAN с hostname.
    """
    ca = VaultCA(_tmp())
    leaf = ca.issue("localhost")

    srv_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    srv_ctx.load_cert_chain(certfile=str(leaf.cert_path), keyfile=str(leaf.key_path))

    cli_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    cli_ctx.load_verify_locations(cadata=ca.ca_cert_pem())
    cli_ctx.check_hostname = True
    cli_ctx.verify_mode = ssl.CERT_REQUIRED

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    server_error: list[Exception] = []

    def serve():
        try:
            raw, _ = listener.accept()
            with srv_ctx.wrap_socket(raw, server_side=True) as ss:
                ss.recv(16)
                ss.sendall(b"ok")
        except Exception as exc:  # noqa: BLE001 — прокидываем в основной поток
            server_error.append(exc)

    t = threading.Thread(target=serve)
    t.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            # server_hostname="localhost" → проверка SAN=DNS:localhost у leaf.
            with cli_ctx.wrap_socket(raw, server_hostname="localhost") as cs:
                cs.sendall(b"ping")
                assert cs.recv(16) == b"ok"
                peer = cs.getpeercert()
        # SAN действительно содержит localhost.
        sans = {v for typ, v in peer.get("subjectAltName", ()) if typ == "DNS"}
        assert "localhost" in sans, f"SAN не содержит localhost: {peer}"
    finally:
        t.join(timeout=5)
        listener.close()
    assert not server_error, f"ошибка TLS-сервера (цепочка невалидна?): {server_error}"
    print("OK VaultCA: leaf подписан корнем — TLS-хендшейк с CA в trust проходит")


def test_leaf_reused_from_disk():
    """Новый VaultCA на том же каталоге отдаёт leaf с диска (не переиздаёт)."""
    d = _tmp()
    first = VaultCA(d).issue("cache.example.com")
    first_bytes = first.cert_path.read_bytes()
    second = VaultCA(d).issue("cache.example.com")
    assert isinstance(second, LeafCert)
    assert second.cert_path.read_bytes() == first_bytes, "leaf переиздан вместо кэша с диска"
    print("OK VaultCA: leaf переиспользуется с диска между запусками")


def main():
    test_ca_generated_and_reused()
    test_ca_key_perms_0600()
    test_leaf_cached_by_host()
    test_leaf_chain_valid_via_tls_handshake()
    test_leaf_reused_from_disk()
    print("ALL VAULT-TLS OK")


if __name__ == "__main__":
    main()
