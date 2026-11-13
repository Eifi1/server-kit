"""The account mails' words, in the seven languages: the account layer writes them once.

``docs/auth-harmonization.md`` §12.3 ("Mail texts") and §12.5 item 10 in ``Eifi1/ui-kit``.
Until 0.29 the words were each app's (§8); the account layer sends the mails itself, so it
writes them, as the ui-kit writes the sign-in's words: German is ``de-CH``, formal, with
"ss", as in the ui-kit's catalogues. They are lifted from Kurvenschmiede's (which had all
seven: ``password_reset_service``, ``email_verification_service``, ``email_change_service``,
``invitation_service``, ``erasure_service``), with the app's name as ``{app}`` and its
product line left out, and from keksdose's passkey notice (``passkey_service._announce_added``),
which had four.

An app may replace a kind with its own table (``AuthDeps.mail_texts``):
:func:`account_mail_texts` merges and checks it, so a placeholder the kit does not fill, or a
table without the fallback language, fails when the router is built rather than at a send.

**The lifetimes are written out** ("one hour", "48 hours", "14 days"), so every language
gets its plural right; they are the kit's :data:`~eifi1_server_kit.auth.RESET_TTL`,
:data:`~eifi1_server_kit.auth.VERIFY_TTL`, :data:`~eifi1_server_kit.auth.EMAIL_CHANGE_TTL`
and :data:`~eifi1_server_kit.auth.INVITE_TTL`, and the kit's tests hold the two together.

The placeholders each kind fills are :data:`ACCOUNT_MAIL_VALUES`: ``{app}`` the app's name,
``{name}`` the recipient's in their own language (:func:`~eifi1_server_kit.auth.full_name`),
``{contact}`` a person to write to (the app's support address), ``{new_email}`` where an
address change's link went, ``{passkey}`` the new passkey's name, ``{date}`` the erasure's day
(:func:`mail_date`), ``{backup_days}`` and ``{log_days}`` the retention note
(:func:`~eifi1_server_kit.user_admin.deletion_mail_retention_note`).
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from datetime import UTC, date, datetime

from eifi1_server_kit.auth.tokens import OneTimeTokenKind
from eifi1_server_kit.mail import AccountNoticeKind, MailText, pick_entry

__all__ = [
    "ACCOUNT_MAIL_KINDS",
    "ACCOUNT_MAIL_TEXTS",
    "ACCOUNT_MAIL_VALUES",
    "AccountMailText",
    "account_mail_texts",
    "mail_date",
]


class AccountMailText(enum.StrEnum):
    """Which account mail a table of words is for — the key of ``AuthDeps.mail_texts``."""

    #: The reset link (``/password-reset/request``), to the account's own address.
    PASSWORD_RESET = "password_reset"
    #: The verification link, after a sign-up and on "Send again".
    VERIFICATION = "verification"
    #: An address change's link, to the NEW address (user-admin §6.2).
    EMAIL_CHANGE = "email_change"
    #: An address change's notice, to the OLD address: told, never handed the link.
    EMAIL_CHANGE_NOTICE = "email_change_notice"
    #: An invitation, in the invitee's language (§4.4).
    INVITATION = "invitation"
    #: The deletion request's confirmation in ``after_days`` mode: the day of the erasure.
    ACCOUNT_DELETION = "account_deletion"
    #: The same in ``operator`` mode: an administrator erases, on no promised day.
    ACCOUNT_DELETION_OPERATOR = "account_deletion_operator"
    #: A passkey was added (keksdose's audit AU-1): a way in no sign-out reaches.
    PASSKEY_ADDED_NOTICE = "passkey_added_notice"


#: The ``kind`` each mail is sent and logged under (:meth:`~eifi1_server_kit.mail.Mailer.send`):
#: a link's by its token kind, a notice's by :class:`~eifi1_server_kit.mail.AccountNoticeKind`.
ACCOUNT_MAIL_KINDS: Mapping[AccountMailText, str] = {
    AccountMailText.PASSWORD_RESET: OneTimeTokenKind.PASSWORD_RESET,
    AccountMailText.VERIFICATION: OneTimeTokenKind.VERIFICATION,
    AccountMailText.EMAIL_CHANGE: OneTimeTokenKind.EMAIL_CHANGE,
    AccountMailText.EMAIL_CHANGE_NOTICE: AccountNoticeKind.EMAIL_CHANGE_NOTICE,
    AccountMailText.INVITATION: OneTimeTokenKind.INVITATION,
    AccountMailText.ACCOUNT_DELETION: AccountNoticeKind.ACCOUNT_DELETION,
    AccountMailText.ACCOUNT_DELETION_OPERATOR: AccountNoticeKind.ACCOUNT_DELETION,
    AccountMailText.PASSKEY_ADDED_NOTICE: AccountNoticeKind.PASSKEY_ADDED_NOTICE,
}

#: The placeholders the account layer fills for each mail; a table may use any of them.
ACCOUNT_MAIL_VALUES: Mapping[AccountMailText, frozenset[str]] = {
    AccountMailText.PASSWORD_RESET: frozenset({"app", "name"}),
    AccountMailText.VERIFICATION: frozenset({"app", "name"}),
    AccountMailText.EMAIL_CHANGE: frozenset({"app", "name"}),
    AccountMailText.EMAIL_CHANGE_NOTICE: frozenset({"app", "name", "new_email", "contact"}),
    AccountMailText.INVITATION: frozenset({"app"}),
    AccountMailText.ACCOUNT_DELETION: frozenset({"app", "name", "date", "contact", "backup_days", "log_days"}),
    AccountMailText.ACCOUNT_DELETION_OPERATOR: frozenset({"app", "name", "contact", "backup_days", "log_days"}),
    AccountMailText.PASSKEY_ADDED_NOTICE: frozenset({"app", "name", "passkey", "contact"}),
}

_RESET: Mapping[str, MailText] = {
    "de-CH": MailText(
        subject="Passwort für {app} zurücksetzen",
        intro="Hallo {name},",
        body=(
            "für Ihr {app}-Konto wurde angefragt, das Passwort zurückzusetzen. Über den Link unten können Sie "
            "ein neues vergeben; er funktioniert einmal und läuft nach einer Stunde ab. Bis dahin bleibt Ihr "
            "bisheriges Passwort gültig, und sobald Sie ihn nutzen, wird jedes angemeldete Gerät abgemeldet."
        ),
        cta="Neues Passwort vergeben",
        outro="Falls die Anfrage nicht von Ihnen kam, ignorieren Sie diese Mail — an Ihrem Konto hat sich nichts geändert.",
    ),
    "en": MailText(
        subject="Reset your {app} password",
        intro="Hello {name},",
        body=(
            "somebody — hopefully you — asked to reset the password of your {app} account. The link below "
            "lets you choose a new one; it works once and expires in one hour. Your current password stays "
            "valid until you use it, and every device signed in to the account is signed out when you do."
        ),
        cta="Choose a new password",
        outro="If you did not ask for this, ignore this mail — nothing has changed on your account.",
    ),
    "es": MailText(
        subject="Restablezca su contraseña de {app}",
        intro="Hola, {name}:",
        body=(
            "Alguien —esperamos que usted— ha solicitado restablecer la contraseña de su cuenta de {app}. El "
            "enlace de abajo le permite elegir una nueva; funciona una sola vez y caduca en una hora. Su "
            "contraseña actual sigue siendo válida hasta que lo utilice, y al hacerlo se cerrará la sesión en "
            "todos los dispositivos conectados a la cuenta."
        ),
        cta="Elegir una contraseña nueva",
        outro="Si no ha sido usted quien lo ha solicitado, ignore este correo: su cuenta no ha cambiado.",
    ),
    "fr": MailText(
        subject="Réinitialisez votre mot de passe {app}",
        intro="Bonjour {name},",
        body=(
            "Quelqu’un — vous, nous l’espérons — a demandé la réinitialisation du mot de passe de votre compte "
            "{app}. Le lien ci-dessous vous permet d’en choisir un nouveau ; il ne fonctionne qu’une fois "
            "et expire au bout d’une heure. Votre mot de passe actuel reste valable jusqu’à ce que vous "
            "l’utilisiez, et tous les appareils connectés au compte seront alors déconnectés."
        ),
        cta="Choisir un nouveau mot de passe",
        outro="Si vous n’êtes pas à l’origine de cette demande, ignorez ce message : votre compte n’a pas été modifié.",
    ),
    "it": MailText(
        subject="Reimpostazione della password di {app}",
        intro="Buongiorno {name},",
        body=(
            "qualcuno, probabilmente Lei, ha chiesto di reimpostare la password del Suo account {app}. Il link "
            "qui sotto Le consente di sceglierne una nuova; funziona una sola volta e scade dopo un’ora. La "
            "password attuale resta valida finché non lo utilizza, e quando lo farà tutti i dispositivi "
            "collegati all’account verranno disconnessi."
        ),
        cta="Scegliere una nuova password",
        outro="Se non è stato Lei a fare questa richiesta, ignori questa e-mail: il Suo account non è stato modificato.",
    ),
    "hu": MailText(
        subject="{app}-jelszó visszaállítása",
        intro="Kedves {name}!",
        body=(
            "Valaki – remélhetőleg Ön – kérte {app}-fiókja jelszavának visszaállítását. Az alábbi hivatkozással "
            "új jelszót választhat; egyszer használható, és egy óra múlva lejár. Jelenlegi jelszava a "
            "hivatkozás felhasználásáig érvényes marad, a felhasználáskor pedig a fiókba bejelentkezett összes "
            "eszközön megszűnik a bejelentkezés."
        ),
        cta="Új jelszó megadása",
        outro="Ha nem Ön kérte, hagyja figyelmen kívül ezt az e-mailt – a fiókja nem változott.",
    ),
    "zh": MailText(
        subject="重置您的 {app} 密码",
        intro="{name}，您好：",
        body=(
            "有人（希望是您本人）申请重置您的 {app} 账户密码。您可以通过下方链接设置新密码；该链接仅可使用一次，"
            "一小时后失效。在您使用该链接之前，当前密码仍然有效；使用之后，所有已登录该账户的设备都将退出登录。"
        ),
        cta="设置新密码",
        outro="如果这不是您本人的操作，请忽略此邮件——您的账户没有发生任何变化。",
    ),
}

_VERIFICATION: Mapping[str, MailText] = {
    "de-CH": MailText(
        subject="Bestätigen Sie Ihre E-Mail-Adresse für {app}",
        intro="Hallo {name},",
        body="bitte bestätigen Sie, dass dies Ihre Adresse für {app} ist. Der Link unten ist 48 Stunden gültig.",
        cta="Adresse bestätigen",
        outro="Falls Sie kein Konto bei {app} angelegt haben, ignorieren Sie diese Mail.",
    ),
    "en": MailText(
        subject="Confirm your email address for {app}",
        intro="Hello {name},",
        body="please confirm that this is your address for {app}. The link below works for 48 hours.",
        cta="Confirm my address",
        outro="If you did not create an account on {app}, ignore this mail.",
    ),
    "es": MailText(
        subject="Confirme su dirección de correo para {app}",
        intro="Hola, {name}:",
        body="Confirme que esta es su dirección para {app}. El enlace de abajo es válido durante 48 horas.",
        cta="Confirmar mi dirección",
        outro="Si no ha creado una cuenta en {app}, ignore este correo.",
    ),
    "fr": MailText(
        subject="Confirmez votre adresse e-mail pour {app}",
        intro="Bonjour {name},",
        body="Veuillez confirmer qu’il s’agit bien de votre adresse pour {app}. Le lien ci-dessous est valable 48 heures.",
        cta="Confirmer mon adresse",
        outro="Si vous n’avez pas créé de compte sur {app}, ignorez ce message.",
    ),
    "it": MailText(
        subject="Confermi il Suo indirizzo e-mail per {app}",
        intro="Buongiorno {name},",
        body="La preghiamo di confermare che questo è il Suo indirizzo per {app}. Il link qui sotto è valido per 48 ore.",
        cta="Confermare l’indirizzo",
        outro="Se non ha creato un account su {app}, ignori questa e-mail.",
    ),
    "hu": MailText(
        subject="{app}: erősítse meg e-mail-címét",
        intro="Kedves {name}!",
        body=(
            "Kérjük, erősítse meg, hogy ez az e-mail-cím az Ön {app}-fiókjához tartozik. Az alábbi hivatkozás "
            "48 óráig érvényes."
        ),
        cta="Cím megerősítése",
        outro="Ha nem Ön hozta létre ezt a(z) {app}-fiókot, hagyja figyelmen kívül ezt az e-mailt.",
    ),
    "zh": MailText(
        subject="确认您在 {app} 的电子邮件地址",
        intro="{name}，您好：",
        body="请确认这是您在 {app} 使用的地址。下方链接在 48 小时内有效。",
        cta="确认我的地址",
        outro="如果您没有在 {app} 创建账户，请忽略此邮件。",
    ),
}

_EMAIL_CHANGE: Mapping[str, MailText] = {
    "de-CH": MailText(
        subject="Bestätigen Sie Ihre neue Adresse für {app}",
        intro="Hallo {name},",
        body=(
            "Sie möchten die Adresse Ihres {app}-Kontos auf diese ändern. Bestätigen Sie sie über den Link "
            "unten; er funktioniert einmal und ist 48 Stunden gültig. Bis dahin behält Ihr Konto seine "
            "bisherige Adresse."
        ),
        cta="Neue Adresse bestätigen",
        outro="Falls Sie das nicht veranlasst haben, ignorieren Sie diese Mail – es ändert sich nichts.",
    ),
    "en": MailText(
        subject="Confirm your new address for {app}",
        intro="Hello {name},",
        body=(
            "you asked to change the address of your {app} account to this one. Confirm it with the link "
            "below; it works once, for 48 hours. Until then your account keeps its current address."
        ),
        cta="Confirm the new address",
        outro="If you did not ask for this, ignore this mail — nothing changes.",
    ),
    "es": MailText(
        subject="Confirme su nueva dirección para {app}",
        intro="Hola, {name}:",
        body=(
            "Ha pedido cambiar la dirección de su cuenta de {app} a esta. Confírmela con el enlace de abajo; "
            "funciona una sola vez durante 48 horas. Hasta entonces, su cuenta conserva su dirección actual."
        ),
        cta="Confirmar la nueva dirección",
        outro="Si no lo ha pedido usted, ignore este correo: no cambia nada.",
    ),
    "fr": MailText(
        subject="Confirmez votre nouvelle adresse pour {app}",
        intro="Bonjour {name},",
        body=(
            "Vous avez demandé à remplacer l’adresse de votre compte {app} par celle-ci. Confirmez-la avec le "
            "lien ci-dessous ; il ne fonctionne qu’une fois, pendant 48 heures. D’ici là, votre compte "
            "garde son adresse actuelle."
        ),
        cta="Confirmer la nouvelle adresse",
        outro="Si vous n’êtes pas à l’origine de cette demande, ignorez ce message : rien ne change.",
    ),
    "it": MailText(
        subject="Confermi il Suo nuovo indirizzo per {app}",
        intro="Buongiorno {name},",
        body=(
            "ha chiesto di cambiare l’indirizzo del Suo account {app} con questo. Lo confermi con il link qui "
            "sotto: funziona una sola volta, per 48 ore. Fino ad allora il Suo account mantiene l’indirizzo "
            "attuale."
        ),
        cta="Confermare il nuovo indirizzo",
        outro="Se non l’ha chiesto Lei, ignori questa e-mail: non cambia nulla.",
    ),
    "hu": MailText(
        subject="Erősítse meg {app}-fiókja új címét",
        intro="Kedves {name}!",
        body=(
            "Azt kérte, hogy {app}-fiókja címe erre változzon. Erősítse meg az alábbi hivatkozással; egyszer "
            "használható, 48 órán át. Addig a fiókja megtartja a jelenlegi címét."
        ),
        cta="Új cím megerősítése",
        outro="Ha nem Ön kérte, hagyja figyelmen kívül ezt az e-mailt – semmi sem változik.",
    ),
    "zh": MailText(
        subject="确认您在 {app} 的新地址",
        intro="{name}，您好：",
        body=(
            "您申请将 {app} 账户的地址更改为此地址。请通过下方链接确认；该链接仅可使用一次，有效期 48 小时。"
            "在此之前，您的账户仍使用当前地址。"
        ),
        cta="确认新地址",
        outro="如果这不是您本人的操作，请忽略此邮件——不会有任何更改。",
    ),
}

_EMAIL_CHANGE_NOTICE: Mapping[str, MailText] = {
    "de-CH": MailText(
        subject="Die Adresse Ihres {app}-Kontos wird geändert",
        intro="Hallo {name},",
        body=(
            "in Ihrem {app}-Konto wurde verlangt, die Adresse auf {new_email} zu ändern. Die Änderung gilt "
            "erst, wenn der dorthin gesendete Link bestätigt wird."
        ),
        cta="{app} öffnen",
        outro="Waren das nicht Sie? Ändern Sie jetzt Ihr Passwort und schreiben Sie an {contact}.",
    ),
    "en": MailText(
        subject="Your {app} address is being changed",
        intro="Hello {name},",
        body=(
            "someone signed in to your {app} account asked to change its address to {new_email}. The change "
            "takes effect only once the link sent there is confirmed."
        ),
        cta="Open {app}",
        outro="Not you? Change your password now and write to {contact}.",
    ),
    "es": MailText(
        subject="Se está cambiando la dirección de su cuenta de {app}",
        intro="Hola, {name}:",
        body=(
            "Desde su cuenta de {app} se ha pedido cambiar su dirección a {new_email}. El cambio solo se "
            "aplica cuando se confirme el enlace enviado allí."
        ),
        cta="Abrir {app}",
        outro="¿No ha sido usted? Cambie ahora su contraseña y escriba a {contact}.",
    ),
    "fr": MailText(
        subject="L’adresse de votre compte {app} est en cours de modification",
        intro="Bonjour {name},",
        body=(
            "Depuis votre compte {app}, quelqu’un a demandé à remplacer son adresse par {new_email}. Le "
            "changement ne prend effet qu’une fois le lien envoyé à cette adresse confirmé."
        ),
        cta="Ouvrir {app}",
        outro="Ce n’était pas vous ? Changez votre mot de passe maintenant et écrivez à {contact}.",
    ),
    "it": MailText(
        subject="L’indirizzo del Suo account {app} sta per cambiare",
        intro="Buongiorno {name},",
        body=(
            "dal Suo account {app} è stato chiesto di cambiare l’indirizzo in {new_email}. La modifica ha "
            "effetto solo quando il link inviato a quell’indirizzo viene confermato."
        ),
        cta="Aprire {app}",
        outro="Non è stato Lei? Cambi subito la password e scriva a {contact}.",
    ),
    "hu": MailText(
        subject="{app}-fiókja címe megváltozik",
        intro="Kedves {name}!",
        body=(
            "{app}-fiókjában kérték, hogy a cím erre változzon: {new_email}. A változás csak akkor lép "
            "életbe, ha az oda küldött hivatkozást megerősítik."
        ),
        cta="{app} megnyitása",
        outro="Nem Ön volt? Változtassa meg most a jelszavát, és írjon ide: {contact}.",
    ),
    "zh": MailText(
        subject="您的 {app} 账户地址正在更改",
        intro="{name}，您好：",
        body="有人在您的 {app} 账户中申请将地址更改为 {new_email}。只有在发往该地址的链接被确认后，更改才会生效。",
        cta="打开 {app}",
        outro="不是您本人？请立即修改密码，并写信至 {contact}。",
    ),
}

_INVITATION: Mapping[str, MailText] = {
    "de-CH": MailText(
        subject="Einladung zu {app}",
        intro="Hallo,",
        body=(
            "Sie sind zu {app} eingeladen. Der Link unten öffnet die Registrierung mit Ihrer Adresse — oder, "
            "wenn Sie schon ein Konto haben, nimmt die Einladung nach der Anmeldung an. Er funktioniert "
            "einmal und ist 14 Tage gültig."
        ),
        cta="Einladung annehmen",
        outro="Falls Ihnen das nichts sagt, ignorieren Sie diese Mail.",
    ),
    "en": MailText(
        subject="You have been invited to {app}",
        intro="Hello,",
        body=(
            "you have been invited to {app}. The link below opens the registration with your address — or, "
            "if you already have an account, accepts the invitation once you sign in. It works once, for "
            "14 days."
        ),
        cta="Accept the invitation",
        outro="If this means nothing to you, ignore this mail.",
    ),
    "es": MailText(
        subject="Invitación a {app}",
        intro="Hola:",
        body=(
            "Le han invitado a {app}. El enlace de abajo abre el registro con su dirección o, si ya tiene una "
            "cuenta, acepta la invitación en cuanto inicie sesión. Funciona una sola vez y durante 14 días."
        ),
        cta="Aceptar la invitación",
        outro="Si esto no le dice nada, ignore este correo.",
    ),
    "fr": MailText(
        subject="Invitation à {app}",
        intro="Bonjour,",
        body=(
            "Vous êtes invité à {app}. Le lien ci-dessous ouvre l’inscription avec votre adresse — ou, si vous "
            "avez déjà un compte, accepte l’invitation dès que vous êtes connecté. Il ne fonctionne qu’une "
            "fois et pendant 14 jours."
        ),
        cta="Accepter l’invitation",
        outro="Si ce message ne vous dit rien, ignorez-le.",
    ),
    "it": MailText(
        subject="Invito a {app}",
        intro="Buongiorno,",
        body=(
            "è stato invitato a {app}. Il link qui sotto apre la registrazione con il Suo indirizzo oppure, se "
            "ha già un account, accetta l’invito dopo l’accesso. Funziona una sola volta e per 14 giorni."
        ),
        cta="Accettare l’invito",
        outro="Se questo messaggio non Le dice nulla, lo ignori.",
    ),
    "hu": MailText(
        subject="Meghívó: {app}",
        intro="Jó napot kívánunk!",
        body=(
            "Meghívták a(z) {app} használatára. Az alábbi hivatkozás megnyitja a regisztrációt az Ön címével "
            "– vagy, ha már van fiókja, bejelentkezés után elfogadja a meghívót. Egyszer használható, 14 napig."
        ),
        cta="Meghívó elfogadása",
        outro="Ha ez semmit sem mond Önnek, hagyja figyelmen kívül ezt az e-mailt.",
    ),
    "zh": MailText(
        subject="邀请您加入 {app}",
        intro="您好：",
        body=(
            "您已受邀加入 {app}。点击下方链接即可使用您的地址注册；如果您已有账户，登录后即会接受邀请。"
            "该链接仅可使用一次，14 天内有效。"
        ),
        cta="接受邀请",
        outro="如果您不清楚这封邮件的来由，请忽略。",
    ),
}

_DELETION: Mapping[str, MailText] = {
    "de-CH": MailText(
        subject="Ihr {app}-Konto wird gelöscht",
        intro="Hallo {name},",
        body=(
            "Sie haben die Löschung Ihres {app}-Kontos verlangt. Es ist ab sofort deaktiviert: Niemand kann "
            "sich mehr anmelden, und jede Sitzung ist beendet. Am {date} wird es endgültig gelöscht. Kopien in "
            "Datenbank-Sicherungen und gelöschten Dateien verschwinden binnen {backup_days} Tagen nach der "
            "Löschung, Server-Protokolle binnen {log_days}."
        ),
        cta="{app} öffnen",
        outro=(
            "Haben Sie es sich anders überlegt, oder kam die Anfrage nicht von Ihnen? Schreiben Sie vor dem "
            "{date} an {contact}."
        ),
    ),
    "en": MailText(
        subject="Your {app} account will be deleted",
        intro="Hello {name},",
        body=(
            "you asked for your {app} account to be deleted. It is deactivated as of now: nobody can sign in "
            "to it, and every session has ended. On {date} it is erased for good. Copies in database backups "
            "and deleted files age out within {backup_days} days of the erasure, server logs within {log_days}."
        ),
        cta="Open {app}",
        outro="Changed your mind, or was it not you? Write to {contact} before {date}.",
    ),
    "es": MailText(
        subject="Su cuenta de {app} se va a eliminar",
        intro="Hola, {name}:",
        body=(
            "Ha pedido que se elimine su cuenta de {app}. Queda desactivada desde ahora: nadie puede iniciar "
            "sesión en ella y todas las sesiones han terminado. El {date} se borrará definitivamente. Las "
            "copias en las copias de seguridad de la base de datos y en los archivos eliminados desaparecen en "
            "{backup_days} días tras el borrado, los registros del servidor en {log_days}."
        ),
        cta="Abrir {app}",
        outro="¿Ha cambiado de opinión o no ha sido usted? Escriba a {contact} antes del {date}.",
    ),
    "fr": MailText(
        subject="Votre compte {app} va être supprimé",
        intro="Bonjour {name},",
        body=(
            "Vous avez demandé la suppression de votre compte {app}. Il est désactivé dès maintenant : "
            "plus personne ne peut s’y connecter, et toutes les sessions ont pris fin. Le {date}, il sera "
            "effacé définitivement. Les copies dans les sauvegardes de la base de données et les fichiers "
            "supprimés disparaissent dans les {backup_days} jours suivant l’effacement, les journaux du serveur "
            "dans les {log_days}."
        ),
        cta="Ouvrir {app}",
        outro="Vous avez changé d’avis, ou ce n’était pas vous ? Écrivez à {contact} avant le {date}.",
    ),
    "it": MailText(
        subject="Il Suo account {app} sta per essere eliminato",
        intro="Buongiorno {name},",
        body=(
            "ha chiesto di eliminare il Suo account {app}. È disattivato da subito: nessuno può più accedervi "
            "e tutte le sessioni sono terminate. Il {date} verrà cancellato definitivamente. Le copie nei "
            "backup del database e nei file eliminati scompaiono entro {backup_days} giorni dalla "
            "cancellazione, i log del server entro {log_days}."
        ),
        cta="Aprire {app}",
        outro="Ha cambiato idea, o non è stato Lei? Scriva a {contact} prima del {date}.",
    ),
    "hu": MailText(
        subject="{app}-fiókja törlésre kerül",
        intro="Kedves {name}!",
        body=(
            "Kérte {app}-fiókja törlését. A fiók mostantól inaktív: senki sem tud bejelentkezni, és minden "
            "munkamenet véget ért. Végleges törlésének napja: {date}. Az adatbázis-mentésekben és a törölt "
            "fájlok között lévő másolatok a törlés után {backup_days} napon belül, a szervernaplók {log_days} "
            "napon belül szűnnek meg."
        ),
        cta="{app} megnyitása",
        outro="Meggondolta magát, vagy nem Ön kérte? Írjon ide a törlés napja ({date}) előtt: {contact}.",
    ),
    "zh": MailText(
        subject="您的 {app} 账户即将删除",
        intro="{name}，您好：",
        body=(
            "您申请删除 {app} 账户。该账户即刻起已停用：任何人都无法登录，所有会话均已结束。它将于 {date} "
            "被永久删除。数据库备份和已删除文件中的副本将在删除后 {backup_days} 天内失效，服务器日志在 "
            "{log_days} 天内失效。"
        ),
        cta="打开 {app}",
        outro="改变主意了，或者这不是您本人的操作？请在 {date} 之前写信至 {contact}。",
    ),
}

_DELETION_OPERATOR: Mapping[str, MailText] = {
    "de-CH": MailText(
        subject="Ihr {app}-Konto wird gelöscht",
        intro="Hallo {name},",
        body=(
            "Sie haben die Löschung Ihres {app}-Kontos verlangt. Es ist ab sofort deaktiviert: Niemand kann "
            "sich mehr anmelden, und jede Sitzung ist beendet. Eine Administratorin oder ein Administrator "
            "wird es endgültig löschen. Kopien in Datenbank-Sicherungen und gelöschten Dateien verschwinden "
            "binnen {backup_days} Tagen nach der Löschung, Server-Protokolle binnen {log_days}."
        ),
        cta="{app} öffnen",
        outro=(
            "Haben Sie es sich anders überlegt, oder kam die Anfrage nicht von Ihnen? Schreiben Sie an "
            "{contact}, bevor es gelöscht ist."
        ),
    ),
    "en": MailText(
        subject="Your {app} account is being deleted",
        intro="Hello {name},",
        body=(
            "you asked for your {app} account to be deleted. It is deactivated as of now: nobody can sign in "
            "to it, and every session has ended. An administrator will erase it for good. Copies in database "
            "backups and deleted files age out within {backup_days} days of the erasure, server logs within "
            "{log_days}."
        ),
        cta="Open {app}",
        outro="Changed your mind, or was it not you? Write to {contact} before it is erased.",
    ),
    "es": MailText(
        subject="Su cuenta de {app} se va a eliminar",
        intro="Hola, {name}:",
        body=(
            "Ha pedido que se elimine su cuenta de {app}. Queda desactivada desde ahora: nadie puede iniciar "
            "sesión en ella y todas las sesiones han terminado. Un administrador la borrará definitivamente. "
            "Las copias en las copias de seguridad de la base de datos y en los archivos eliminados desaparecen "
            "en {backup_days} días tras el borrado, los registros del servidor en {log_days}."
        ),
        cta="Abrir {app}",
        outro="¿Ha cambiado de opinión o no ha sido usted? Escriba a {contact} antes de que se borre.",
    ),
    "fr": MailText(
        subject="Votre compte {app} va être supprimé",
        intro="Bonjour {name},",
        body=(
            "Vous avez demandé la suppression de votre compte {app}. Il est désactivé dès maintenant : "
            "plus personne ne peut s’y connecter, et toutes les sessions ont pris fin. Un administrateur "
            "l’effacera définitivement. Les copies dans les sauvegardes de la base de données et les fichiers "
            "supprimés disparaissent dans les {backup_days} jours suivant l’effacement, les journaux du serveur "
            "dans les {log_days}."
        ),
        cta="Ouvrir {app}",
        outro="Vous avez changé d’avis, ou ce n’était pas vous ? Écrivez à {contact} avant qu’il ne soit effacé.",
    ),
    "it": MailText(
        subject="Il Suo account {app} sta per essere eliminato",
        intro="Buongiorno {name},",
        body=(
            "ha chiesto di eliminare il Suo account {app}. È disattivato da subito: nessuno può più accedervi "
            "e tutte le sessioni sono terminate. Un amministratore lo cancellerà definitivamente. Le copie nei "
            "backup del database e nei file eliminati scompaiono entro {backup_days} giorni dalla "
            "cancellazione, i log del server entro {log_days}."
        ),
        cta="Aprire {app}",
        outro="Ha cambiato idea, o non è stato Lei? Scriva a {contact} prima che venga cancellato.",
    ),
    "hu": MailText(
        subject="{app}-fiókja törlésre kerül",
        intro="Kedves {name}!",
        body=(
            "Kérte {app}-fiókja törlését. A fiók mostantól inaktív: senki sem tud bejelentkezni, és minden "
            "munkamenet véget ért. Egy adminisztrátor véglegesen törli. Az adatbázis-mentésekben és a törölt "
            "fájlok között lévő másolatok a törlés után {backup_days} napon belül, a szervernaplók {log_days} "
            "napon belül szűnnek meg."
        ),
        cta="{app} megnyitása",
        outro="Meggondolta magát, vagy nem Ön kérte? Írjon a törlés előtt ide: {contact}.",
    ),
    "zh": MailText(
        subject="您的 {app} 账户即将删除",
        intro="{name}，您好：",
        body=(
            "您申请删除 {app} 账户。该账户即刻起已停用：任何人都无法登录，所有会话均已结束。一位管理员将把它永久"
            "删除。数据库备份和已删除文件中的副本将在删除后 {backup_days} 天内失效，服务器日志在 {log_days} "
            "天内失效。"
        ),
        cta="打开 {app}",
        outro="改变主意了，或者这不是您本人的操作？请在删除之前写信至 {contact}。",
    ),
}

_PASSKEY_ADDED: Mapping[str, MailText] = {
    "de-CH": MailText(
        subject="Ihrem {app}-Konto wurde ein Passkey hinzugefügt",
        intro="Hallo {name},",
        body=(
            "Ihrem Konto wurde soeben ein Passkey mit dem Namen «{passkey}» hinzugefügt. Er meldet sich ohne "
            "Ihr Passwort an."
        ),
        cta="Passkeys ansehen",
        outro=(
            "Waren Sie das nicht? Ändern Sie sofort Ihr Passwort und löschen Sie diesen Passkey in den "
            "Einstellungen – ein neues Passwort allein sperrt ihn nicht aus. Fragen: {contact}."
        ),
    ),
    "en": MailText(
        subject="A passkey was added to your {app} account",
        intro="Hello {name},",
        body="a passkey called “{passkey}” was just added to your account. It signs in without your password.",
        cta="Review your passkeys",
        outro=(
            "Not you? Change your password straight away and delete this passkey in the settings — a new "
            "password alone does not shut it out. Questions: {contact}."
        ),
    ),
    "es": MailText(
        subject="Se ha añadido una clave de acceso a su cuenta de {app}",
        intro="Hola, {name}:",
        body=(
            "Se acaba de añadir a su cuenta una clave de acceso llamada «{passkey}». Permite iniciar sesión sin "
            "su contraseña."
        ),
        cta="Ver sus claves de acceso",
        outro=(
            "¿No ha sido usted? Cambie su contraseña de inmediato y elimine esta clave de acceso en los "
            "ajustes: una contraseña nueva no basta para excluirla. Preguntas: {contact}."
        ),
    ),
    "fr": MailText(
        subject="Une clé d’accès a été ajoutée à votre compte {app}",
        intro="Bonjour {name},",
        body=(
            "Une clé d’accès nommée « {passkey} » vient d’être ajoutée à votre compte. Elle permet de "
            "se connecter sans votre mot de passe."
        ),
        cta="Voir vos clés d’accès",
        outro=(
            "Ce n’était pas vous ? Changez immédiatement votre mot de passe et supprimez cette clé "
            "d’accès dans les réglages — un nouveau mot de passe ne suffit pas à l’exclure. "
            "Questions : {contact}."
        ),
    ),
    "it": MailText(
        subject="Al Suo account {app} è stata aggiunta una passkey",
        intro="Buongiorno {name},",
        body=(
            "al Suo account è stata appena aggiunta una passkey chiamata «{passkey}». Permette di accedere "
            "senza la Sua password."
        ),
        cta="Vedere le passkey",
        outro=(
            "Non è stato Lei? Cambi subito la password ed elimini questa passkey nelle impostazioni: una nuova "
            "password da sola non la esclude. Domande: {contact}."
        ),
    ),
    "hu": MailText(
        subject="Új azonosítókulcs került {app}-fiókjába",
        intro="Kedves {name}!",
        body=(
            "Fiókjához épp most adtak hozzá egy „{passkey}” nevű azonosítókulcsot. Ezzel jelszó nélkül lehet "
            "bejelentkezni."
        ),
        cta="Azonosítókulcsok megtekintése",
        outro=(
            "Nem Ön volt? Azonnal változtassa meg a jelszavát, és törölje ezt az azonosítókulcsot a "
            "beállításokban – egy új jelszó egymagában nem zárja ki. Kérdések: {contact}."
        ),
    ),
    "zh": MailText(
        subject="您的 {app} 账户新增了一个通行密钥",
        intro="{name}，您好：",
        body="您的账户刚刚新增了一个名为“{passkey}”的通行密钥。它无需密码即可登录。",
        cta="查看通行密钥",
        outro="不是您本人？请立即修改密码，并在设置中删除此通行密钥——仅修改密码无法将其排除。如有疑问：{contact}。",
    ),
}

#: The kit's words for every account mail, seven languages each.
ACCOUNT_MAIL_TEXTS: Mapping[AccountMailText, Mapping[str, MailText]] = {
    AccountMailText.PASSWORD_RESET: _RESET,
    AccountMailText.VERIFICATION: _VERIFICATION,
    AccountMailText.EMAIL_CHANGE: _EMAIL_CHANGE,
    AccountMailText.EMAIL_CHANGE_NOTICE: _EMAIL_CHANGE_NOTICE,
    AccountMailText.INVITATION: _INVITATION,
    AccountMailText.ACCOUNT_DELETION: _DELETION,
    AccountMailText.ACCOUNT_DELETION_OPERATOR: _DELETION_OPERATOR,
    AccountMailText.PASSKEY_ADDED_NOTICE: _PASSKEY_ADDED,
}


def _check_table(kind: AccountMailText, table: Mapping[str, MailText]) -> None:
    try:
        pick_entry(None, table)
    except KeyError as exc:
        raise ValueError(f"the {kind} mail's table has no fallback language (de-CH, de or en)") from exc
    sample = {name: name for name in ACCOUNT_MAIL_VALUES[kind]}
    for locale, text in table.items():
        for part in (text.subject, text.intro, text.body, text.cta, text.outro):
            try:
                part.format_map(sample)
            except (KeyError, ValueError, IndexError) as exc:
                raise ValueError(
                    f"the {kind} mail's {locale!r} row uses a placeholder the kit does not fill: {exc}; "
                    f"it fills {', '.join(sorted(ACCOUNT_MAIL_VALUES[kind]))}"
                ) from exc


def account_mail_texts(
    overrides: Mapping[AccountMailText | str, Mapping[str, MailText]] | None = None,
) -> Mapping[AccountMailText, Mapping[str, MailText]]:
    """The kit's tables with the app's own in place of the kinds it names, checked: an
    app's table needs a fallback row (``de-CH``, ``de`` or ``en``, as
    :func:`~eifi1_server_kit.mail.pick` falls back) and may use only the kind's
    :data:`ACCOUNT_MAIL_VALUES`; either fault is a :class:`ValueError`, raised when the
    router is built. A kind the kit does not know is one too."""
    merged: dict[AccountMailText, Mapping[str, MailText]] = dict(ACCOUNT_MAIL_TEXTS)
    for key, table in (overrides or {}).items():
        try:
            kind = AccountMailText(key)
        except ValueError as exc:
            raise ValueError(f"no account mail is called {key!r}") from exc
        _check_table(kind, table)
        merged[kind] = table
    return merged


#: How each language writes a day in a mail's sentence: numeric, so no month name needs a
#: translation; English as ISO 8601, which no reader mistakes for another order.
_DATE_FORMATS: Mapping[str, str] = {
    "de": "%d.%m.%Y",
    "fr": "%d/%m/%Y",
    "it": "%d/%m/%Y",
    "es": "%d/%m/%Y",
    "hu": "%Y. %m. %d.",
    "zh": "%Y年%m月%d日",
}


def mail_date(when: date | datetime, locale: str | None) -> str:
    """The day ``when`` falls on, as ``locale``'s language writes it in a sentence:
    ``13.12.2026`` (de), ``13/12/2026`` (fr, it, es), ``2026. 12. 13.`` (hu),
    ``2026年12月13日`` (zh), ``2026-12-13`` (en and any other). An aware datetime is read in
    UTC; the erasure job runs on UTC days."""
    language = (locale or "").strip().lower().replace("_", "-").split("-", 1)[0]
    if isinstance(when, datetime):
        when = (when.astimezone(UTC) if when.tzinfo is not None else when).date()
    return when.strftime(_DATE_FORMATS.get(language, "%Y-%m-%d"))
