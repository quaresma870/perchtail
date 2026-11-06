"""Password policy (#245): what a new local password must satisfy, set under
Settings -> System. Defaults follow NIST SP 800-63B: length over
composition, no composition rules or expiry unless an admin turns them on.

"Too simple" is decided by rules, not by a downloaded list of common
passwords:
- one character, or a short block, repeated (aaaaaa, abcabc);
- made only of sequences (123456, abcdef, also reversed), keyboard runs
  (qwerty, asdfgh, 1qaz2wsx) and repeated characters;
- too few distinct characters for its length;
- the username or email, give or take case and digits or symbols added.

Errors name the rule that failed and never echo the password.

The policy applies whenever a password is set (own change, first-login
change, admin-created accounts, resets); existing passwords are not checked
retroactively. History keeps only previous argon2id hashes."""

import secrets
import string
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import timedelta

from sqlmodel import Session, col, delete, select

from app import system_settings
from app.auth.models import PasswordHistory, User
from app.timeutils import utcnow

# Bounds for the settings themselves (the API validates against these).
MIN_LENGTH_FLOOR = 8  # NIST's minimum for a chosen password
MAX_LENGTH_CEILING = 1024
MAX_LENGTH_FLOOR = 64  # NIST: allow at least 64 characters
MAX_HISTORY = 24
MAX_AGE_DAYS_CEILING = 3650

# Short pieces of a personal identifier (a 2-letter username) would reject
# too much; only parts at least this long count.
_MIN_PERSONAL_PART = 3
# Pieces of two characters count, so a password that ends a run with a
# short tail (qwertyuiop12, 1234567890ab) is still caught.
_MIN_RUN = 2
_SEQUENCES = (
    "0123456789",
    "abcdefghijklmnopqrstuvwxyz",
    # Keyboard rows (US and the common AZERTY/QWERTZ variants) and columns.
    "1234567890-=",
    "qwertyuiop[]",
    "asdfghjkl;'",
    "zxcvbnm,./",
    "azertyuiop",
    "qsdfghjklm",
    "wxcvbn",
    "qwertzuiop",
    "yxcvbnm",
    "1qaz2wsx3edc4rfv5tgb6yhn7ujm8ik,9ol.0p;/",
    "!qaz@wsx#edc$rfv%tgb^yhn&ujm*ik<(ol>)p:?",
)
_ALL_SEQUENCES = _SEQUENCES + tuple(s[::-1] for s in _SEQUENCES)
_SYMBOLS = set(string.punctuation + " ")


class PasswordPolicyError(ValueError):
    """A password the policy refuses. `rule` is a stable code for the
    frontend; str(error) is the message for the user."""

    def __init__(self, rule: str, message: str):
        super().__init__(message)
        self.rule = rule


@dataclass(frozen=True)
class PasswordPolicy:
    min_length: int = 12
    max_length: int = 128
    reject_simple: bool = True
    reject_personal: bool = True
    history: int = 0
    required_classes: int = 0
    max_age_days: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def get_policy(session: Session) -> PasswordPolicy:
    get_int, get_bool = system_settings.get_int, system_settings.get_bool
    return PasswordPolicy(
        min_length=get_int(session, "password_min_length"),
        max_length=get_int(session, "password_max_length"),
        reject_simple=get_bool(session, "password_reject_simple"),
        reject_personal=get_bool(session, "password_reject_personal"),
        history=get_int(session, "password_history"),
        required_classes=get_int(session, "password_required_classes"),
        max_age_days=get_int(session, "password_max_age_days"),
    )


# --- the rules -------------------------------------------------------------


def _character_classes(password: str) -> int:
    return sum(
        (
            any(c.islower() for c in password),
            any(c.isupper() for c in password),
            any(c.isdigit() for c in password),
            any(not c.isalnum() for c in password),
        )
    )


def _is_repetition(password: str) -> bool:
    """One character or a block of at most half the length, repeated (the
    last copy may be cut short): aaaaaa, abcabc, abcabca."""
    size = len(password)
    for block in range(1, size // 2 + 1):
        unit = password[:block]
        if (unit * (size // block + 1))[:size] == password:
            return True
    return False


def _run_length_at(text: str, start: int) -> int:
    """The longest stretch of `text` from `start` that is a run of one
    character or a piece of a known sequence, or 0 below _MIN_RUN."""
    best = 0
    same = start
    while same < len(text) and text[same] == text[start]:
        same += 1
    best = same - start
    for sequence in _ALL_SEQUENCES:
        index = sequence.find(text[start])
        while index != -1:
            length = 0
            while (
                start + length < len(text)
                and index + length < len(sequence)
                and text[start + length] == sequence[index + length]
            ):
                length += 1
            best = max(best, length)
            index = sequence.find(text[start], index + 1)
    return best if best >= _MIN_RUN else 0


def _is_only_runs(password: str) -> bool:
    """Made entirely of sequences, keyboard runs and repeated characters:
    123456, qwerty123456, 1qaz2wsx, abcdef1111. Case is ignored."""
    text = password.lower()
    position = 0
    while position < len(text):
        length = _run_length_at(text, position)
        if not length:
            return False
        position += length
    return True


def _too_few_distinct(password: str) -> bool:
    """Fewer distinct characters than a third of the length (at least 3,
    at most 8 needed): aabbaabbaabb."""
    needed = max(3, min(8, len(password) // 3))
    return len(set(password)) < needed


def _personal_parts(user_identifiers: Sequence[str]) -> list[str]:
    """The username (for an email, its local part only: everyone at
    example.com shouldn't be barred from "com"), and each word of it or of a
    display name, lowercased, long enough to count."""
    parts: set[str] = set()
    for identifier in user_identifiers:
        value = (identifier or "").strip().lower()
        if "@" in value:
            value = value.split("@", 1)[0]
        if not value:
            continue
        if len(value) >= _MIN_PERSONAL_PART:
            parts.add(value)
        # Single words need one more letter, so a name like "Bob The Admin"
        # doesn't bar every password containing "the".
        words = value.replace(".", " ").replace("_", " ").replace("-", " ").split()
        parts.update(word for word in words if len(word) > _MIN_PERSONAL_PART)
    return sorted(parts)


def _strip_decoration(password: str) -> str:
    """Lowercased, without digits and symbols at either end: Admin2024! ->
    admin."""
    return password.lower().strip(string.digits + string.punctuation + " ")


def check_password(
    password: str, policy: PasswordPolicy, *, user_identifiers: Sequence[str] = ()
) -> None:
    """Raises PasswordPolicyError with the first rule the password breaks.
    `user_identifiers` are the account's username, email and display name.
    History is checked separately (check_history), since it needs the
    account's hashes."""
    if len(password) < policy.min_length:
        raise PasswordPolicyError("too_short", f"Use at least {policy.min_length} characters.")
    if len(password) > policy.max_length:
        raise PasswordPolicyError("too_long", f"Use at most {policy.max_length} characters.")
    if policy.required_classes and _character_classes(password) < policy.required_classes:
        raise PasswordPolicyError(
            "character_classes",
            f"Use at least {policy.required_classes} of: lowercase letters, uppercase letters, "
            "digits, symbols.",
        )
    personal = _personal_parts(user_identifiers)
    if policy.reject_simple:
        if _is_repetition(password):
            raise PasswordPolicyError(
                "repetition", "Too simple: it repeats one character or a short block."
            )
        if _is_only_runs(password):
            raise PasswordPolicyError(
                "sequence",
                "Too simple: it's made of sequences or keyboard runs (like 123456 or qwerty).",
            )
        if _too_few_distinct(password):
            raise PasswordPolicyError(
                "few_distinct", "Too simple: it uses too few different characters."
            )
        whole = {(identifier or "").strip().lower() for identifier in user_identifiers}
        if _strip_decoration(password) in personal or _strip_decoration(password) in whole:
            raise PasswordPolicyError(
                "is_username", "Too simple: it's your username or email with small changes."
            )
    if policy.reject_personal:
        lowered = password.lower()
        if any(part in lowered for part in personal):
            raise PasswordPolicyError(
                "contains_personal", "Don't include your username, email or name."
            )


def user_identifiers(user: User) -> list[str]:
    """What check_password compares against for this account: the
    username, email and display name (#243)."""
    return [value for value in (user.username, user.email, user.display_name) if value]


def check_history(session: Session, user: User, password: str, policy: PasswordPolicy) -> None:
    """Refuses the current password and the previous ones, `history` in
    all, when the history rule is on."""
    if policy.history <= 0:
        return
    from app.auth.providers.local import verify_hash  # avoid an import cycle

    hashes = [user.password_hash] if user.password_hash else []
    hashes += session.exec(
        select(PasswordHistory.password_hash)
        .where(PasswordHistory.user_id == user.id)
        .order_by(col(PasswordHistory.created_at).desc(), col(PasswordHistory.id).desc())
        .limit(max(policy.history - 1, 0))
    ).all()
    if any(verify_hash(stored, password) for stored in hashes[: policy.history]):
        raise PasswordPolicyError(
            "reused",
            f"Choose a password you haven't used for your last {policy.history} passwords.",
        )


def remember_previous(session: Session, user: User, policy: PasswordPolicy) -> None:
    """Before a password is replaced: keeps the outgoing hash when the
    history rule is on, and trims the history to what the rule needs (none
    at all when it's off)."""
    keep = max(policy.history - 1, 0)
    if keep and user.password_hash and user.id is not None:
        session.add(
            PasswordHistory(user_id=user.id, password_hash=user.password_hash, created_at=utcnow())
        )
        session.flush()
    if user.id is not None:
        _trim(session, user.id, keep)


def _trim(session: Session, user_id: int, keep: int) -> None:
    kept_ids = session.exec(
        select(PasswordHistory.id)
        .where(PasswordHistory.user_id == user_id)
        .order_by(col(PasswordHistory.created_at).desc(), col(PasswordHistory.id).desc())
        .limit(keep)
    ).all()
    session.exec(
        delete(PasswordHistory).where(
            PasswordHistory.user_id == user_id, col(PasswordHistory.id).not_in(kept_ids)
        )
    )


def trim_all_history(session: Session, history: int) -> None:
    """After the history setting changes: keeps what the new setting needs
    for every account, and nothing when it's turned off. Doesn't commit."""
    keep = max(history - 1, 0)
    user_ids = session.exec(select(PasswordHistory.user_id).distinct()).all()
    for user_id in user_ids:
        _trim(session, user_id, keep)


def password_expired(user: User, policy: PasswordPolicy) -> bool:
    """A local password older than max_age_days, when expiry is on. An
    account with no recorded change date (an SSO account) never expires."""
    if policy.max_age_days <= 0 or user.password_hash is None:
        return False
    if user.password_changed_at is None:
        return False
    return utcnow() - user.password_changed_at > timedelta(days=policy.max_age_days)


def generate_password(policy: PasswordPolicy, *, user_identifiers: Sequence[str] = ()) -> str:
    """A random temporary password the policy accepts: at least 16
    characters, with every character class, so it passes whatever is
    required."""
    length = min(max(policy.min_length, 16), policy.max_length)
    alphabet = string.ascii_letters + string.digits + "-_.!@#%+="
    pools = (string.ascii_lowercase, string.ascii_uppercase, string.digits, "-_.!@#%+=")
    for _ in range(100):
        chars = [secrets.choice(pool) for pool in pools]
        chars += [secrets.choice(alphabet) for _ in range(length - len(chars))]
        secrets.SystemRandom().shuffle(chars)
        candidate = "".join(chars)
        try:
            check_password(candidate, policy, user_identifiers=user_identifiers)
        except PasswordPolicyError:
            continue
        return candidate
    raise RuntimeError("couldn't generate a password that satisfies the policy")
