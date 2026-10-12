"""Постоянное соответствие идентификаторов ВКонтакте внутренним ключам БД (общая реализация — app.platforms.identities)."""
from app.platforms.builtin import VK
from app.platforms.identities import IdentityStore

store = IdentityStore(VK)
valid_external_id = store.valid_external_id
lookup = store.lookup
external = store.external
register = store.register
