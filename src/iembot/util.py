"""Utility functions for IEMBot"""
import datetime
from html import unescape
import re
import os
import socket
import json
import glob
import pickle
from email.mime.text import MIMEText
import time
import traceback
import pwd
from io import BytesIO

import pytz
import twitter
from oauth import oauth
from twisted.internet import reactor
from twisted.mail import smtp
from twisted.python import log
import twisted.web.error as weberror
from twisted.words.xish import domish
from pyiem.util import utc
from pyiem.reference import TWEET_CHARS


def tweet(bot, oauth_token, twttxt, twitter_media):
    """Blocking tweet method."""
    api = twitter.Api(
        consumer_key=bot.config["bot.twitter.consumerkey"],
        consumer_secret=bot.config["bot.twitter.consumersecret"],
        access_token_key=oauth_token.key,
        access_token_secret=oauth_token.secret,
    )
    try:
        res = api.PostUpdate(twttxt, media=twitter_media)
    except twitter.error.TwitterError as exp:
        # Something bad happened with submitting this to twitter
        if str(exp).startswith("media type unrecognized"):
            # The media content hit some error, just send it without it
            log.msg(
                "Sending '%s' as media to twitter failed, stripping" % (
                    twitter_media,
                )
            )
            res = api.PostUpdate(twttxt)
        else:
            log.err(exp)
            # Since this called from a thread, sleeping should not jam us up
            time.sleep(10)
            res = api.PostUpdate(twttxt, media=twitter_media)
    except Exception as exp:
        log.err(exp)
        # Since this called from a thread, sleeping should not jam us up
        time.sleep(10)
        res = api.PostUpdate(twttxt)
    return res


def channels_room_list(bot, room):
    """
    Send a listing of channels that the room is subscribed to...
    @param room to list
    """
    channels = []
    for channel in bot.routingtable.keys():
        if room in bot.routingtable[channel]:
            channels.append(channel)

    # Need to add a space in the channels listing so that the string does
    # not get so long that it causes chat clients to bail
    msg = "This room is subscribed to %s channels (%s)" % (
        len(channels),
        ", ".join(channels),
    )
    bot.send_groupchat(room, msg)


def channels_room_add(txn, bot, room, channel):
    """Add a channel subscription to a chatroom

    Args:
        txn (psycopg2.transaction): database transaction
        bot (iembot.Basicbot): bot instance
        room (str): the chatroom to add the subscription to
        channel (str): the channel to subscribe to for the room
    """
    # Remove extraneous fluff, all channels are uppercase
    channel = channel.upper().strip().replace(" ", "")
    if channel == "":
        bot.send_groupchat(
            room,
            (
                "Failed to add channel to room "
                "subscription, you supplied a "
                "blank channel?"
            ),
        )
        return
    # Allow channels to be comma delimited
    for ch in channel.split(","):
        if ch not in bot.routingtable:
            bot.routingtable[ch] = []
        # If we are already subscribed, let em know!
        if room in bot.routingtable[ch]:
            bot.send_groupchat(
                room,
                (
                    "Error adding subscription, your "
                    "room is already subscribed to the"
                    "'%s' channel"
                )
                % (ch,),
            )
            continue
        # Add a channels entry for this channel, if one currently does
        # not exist
        txn.execute(
            f"SELECT * from {bot.name}_channels WHERE id = %s",
            (ch,),
        )
        if txn.rowcount == 0:
            txn.execute(
                f"INSERT into {bot.name}_channels(id, name) VALUES (%s, %s)",
                (ch, ch),
            )

        # Add to routing table
        bot.routingtable[ch].append(room)
        # Add to database
        txn.execute(
            f"INSERT into {bot.name}_room_subscriptions "
            "(roomname, channel) VALUES (%s, %s)",
            (room, ch),
        )
        bot.send_groupchat(room, f"Subscribed {room} to channel '{ch}'")
    # Send room a listing of channels!
    channels_room_list(bot, room)


def channels_room_del(txn, bot, room, channel):
    """Removes a channel subscription for a given room

    Args:
        txn (psycopg2.transaction): database cursor
        room (str): room to unsubscribe
        channel (str): channel to unsubscribe from
    """
    channel = channel.upper().strip().replace(" ", "")
    if channel == "":
        bot.send_groupchat(room, "Blank or missing channel")
        return

    for ch in channel.split(","):
        if ch not in bot.routingtable:
            bot.send_groupchat(room, "Unknown channel: '%s'" % (ch,))
            continue

        if room not in bot.routingtable[ch]:
            bot.send_groupchat(
                room, ("Room not subscribed to channel: '%s'") % (ch,)
            )
            continue

        # Remove from routing table
        bot.routingtable[ch].remove(room)
        # Remove from database
        txn.execute(
            f"DELETE from {bot.name}_room_subscriptions WHERE "
            "roomname = %s and channel = %s",
            (room, ch),
        )
        bot.send_groupchat(room, f"Unscribed {room} to channel '{ch}'")
    channels_room_list(bot, room)


def purge_logs(bot):
    """ Remove chat logs on a 24 HR basis """
    log.msg("purge_logs() called...")
    basets = utc() - datetime.timedelta(
        days=int(bot.config.get("bot.purge_xmllog_days", 7))
    )
    for fn in glob.glob("logs/xmllog.*"):
        ts = datetime.datetime.strptime(fn, "logs/xmllog.%Y_%m_%d")
        ts = ts.replace(tzinfo=pytz.UTC)
        if ts < basets:
            log.msg("Purging logfile %s" % (fn,))
            os.remove(fn)


def email_error(exp, bot, message=""):
    """
    Something to email errors when something fails
    """
    # Always log a message about our fun
    cstr = BytesIO()
    if isinstance(exp, Exception):
        traceback.print_exc(file=cstr)
        cstr.seek(0)
        if isinstance(exp, Exception):
            log.err(exp)
        else:
            log.msg(exp)
    log.msg(message)

    def should_email():
        """Should we send an email?"""
        # bot.email_timestamps contains timestamps of emails we *sent*
        utcnow = utc()
        # If we don't have any entries, we should email!
        if len(bot.email_timestamps) < 10:
            bot.email_timestamps.insert(0, utcnow)
            return True
        delta = utcnow - bot.email_timestamps[-1]
        # Effectively limits to 10 per hour
        if delta < datetime.timedelta(hours=1):
            return False
        # We are going to email!
        bot.email_timestamps.insert(0, utcnow)
        # trim listing to 10 entries
        while len(bot.email_timestamps) > 10:
            bot.email_timestamps.pop()
        return True

    # Logic to prevent email bombs
    if not should_email():
        log.msg("Email threshold exceeded, so no email sent!")
        return False

    msg = MIMEText(
        """
System          : %s@%s [CWD: %s]
System UTC date : %s
process id      : %s
system load     : %s
Exception       :
%s
%s

Message:
%s"""
        % (
            pwd.getpwuid(os.getuid())[0],
            socket.gethostname(),
            os.getcwd(),
            utc(),
            os.getpid(),
            " ".join(["%.2f" % (_,) for _ in os.getloadavg()]),
            cstr.read(),
            exp,
            message,
        )
    )

    msg["subject"] = "[bot] Traceback -- %s" % (socket.gethostname(),)

    msg["From"] = bot.config.get("bot.email_errors_from", "root@localhost")
    msg["To"] = bot.config.get("bot.email_errors_to", "root@localhost")

    df = smtp.sendmail(
        bot.config.get("bot.smtp_server", "localhost"),
        msg["From"],
        msg["To"],
        msg,
    )
    df.addErrback(log.err)
    return True


def disable_twitter_user(bot, user_id, errcode=0):
    """Disable the twitter subs for this user_id

    Args:
        user_id (big_id): The twitter user to disable
        errcode (int): The twitter errorcode
    """
    twuser = bot.tw_users.get(user_id)
    if twuser is None:
        log.msg(f"Failed to disable unknown twitter user_id {user_id}")
        return False
    screen_name = twuser["screen_name"]
    if screen_name.startswith("iembot_"):
        log.msg(f"Skipping disabling of twitter for {user_id} ({screen_name})")
        return False
    bot.tw_users.pop(user_id, None)
    log.msg(
        f"Removing twitter access token for user: {user_id} ({screen_name}) "
        f"errcode: {errcode}"
    )
    df = bot.dbpool.runOperation(
        f"UPDATE {bot.name}_twitter_oauth SET updated = now(), "
        "access_token = null, access_token_secret = null "
        "WHERE user_id = %s",
        (user_id, ),
    )
    df.addErrback(log.err)
    return True


def tweet_cb(response, bot, twttxt, room, myjid, user_id):
    """
    Called after success going to twitter
    """
    if response is None:
        return
    twuser = bot.tw_users.get(user_id)
    if twuser is None:
        return response
    screen_name = twuser["screen_name"]
    if isinstance(response, twitter.Status):
        url = "https://twitter.com/%s/status/%s" % (screen_name, response.id)
    else:
        url = "https://twitter.com/%s/status/%s" % (screen_name, response)

    # Log
    df = bot.dbpool.runOperation(
        f"INSERT into {bot.name}_social_log(medium, source, resource_uri, "
        "message, response, response_code) values (%s,%s,%s,%s,%s,%s)",
        ("twitter", myjid, url, twttxt, repr(response), 200),
    )
    df.addErrback(log.err)
    return response


def twitter_errback(err, bot, user_id, msg):
    """Error callback when simple twitter workflow fails."""
    # err is class twisted.python.failure.Failure
    log.err(err)
    try:
        val = err.value.message
        errcode = val[0].get("code", 0)
        if errcode in [89, 185, 326, 64]:
            # 89: Expired token, so we shall revoke for now
            # 185: User is over quota
            # 326: User is temporarily locked out
            # 64: User is suspended
            if disable_twitter_user(bot, user_id, errcode):
                return
    except Exception as exp:
        log.err(exp)
    email_error(err, bot, msg)


def tweet_eb(
    err, bot, twttxt, access_token, room, myjid, user_id, twtextra, trip
):
    """
    Called after error going to twitter
    """
    log.msg("--> tweet_eb called")

    # Make sure we only are trapping API errors
    err.trap(weberror.Error)
    # Don't email duplication errors
    j = {}
    try:
        j = json.loads(err.value.response.decode("utf-8", "ignore"))
    except Exception as exp:
        log.msg(
            "Unable to parse response |%s| as JSON %s"
            % (err.value.response, exp)
        )
    if j.get("errors", []):
        errcode = j["errors"][0].get("code", 0)
        if errcode in [130, 131]:
            # 130: over capacity
            # 131: Internal error
            reactor.callLater(
                15,  # @UndefinedVariable
                bot.tweet,
                twttxt,
                access_token,
                room,
                myjid,
                user_id,
                twtextra,
                trip + 1,
            )
            return
        if errcode in [89, 185, 326, 64]:
            # 89: Expired token, so we shall revoke for now
            # 185: User is over quota
            # 326: User is temporarily locked out
            # 64: User is suspended
            disable_twitter_user(bot, user_id, errcode)
        if errcode not in [187]:
            # 187 duplicate message
            email_error(
                err,
                bot,
                ("Room: %s\nmyjid: %s\nuser_id: %s\n" "tweet: %s\nError:%s\n")
                % (room, myjid, user_id, twttxt, err.value.response),
            )

    log.msg(err.getErrorMessage())
    log.msg(err.value.response)

    # Log this
    deffered = bot.dbpool.runOperation(
        f"INSERT into {bot.name}_social_log(medium, source, message, "
        "response, response_code, resource_uri) values (%s,%s,%s,%s,%s,%s)",
        (
            "twitter",
            myjid,
            twttxt,
            err.value.response,
            int(err.value.status),
            user_id,
        ),
    )
    deffered.addErrback(log.err)

    # return err.value.response


def fbfail(err, bot, room, myjid, message, fbpage):
    """ We got a failure from facebook API!"""
    log.msg("=== Facebook API Failure ===")
    log.err(err)
    err.trap(weberror.Error)
    j = None
    try:
        j = json.loads(err.value.response)
    except Exception as exp:
        log.err(exp)
    log.msg(err.getErrorMessage())
    log.msg(err.value.response)
    bot.email_error(
        err,
        ("FBError room: %s\nmyjid: %s\nmessage: %s\n" "Error:%s")
        % (room, myjid, message, err.value.response),
    )

    msg = "Posting to facebook failed! Got this message: %s" % (
        err.getErrorMessage(),
    )
    if j is not None:
        msg = "Posting to facebook failed with this message: %s" % (
            j.get("error", {}).get("message", "Missing"),
        )

    if room is not None:
        bot.send_groupchat(room, msg)

    # Log this
    df = bot.dbpool.runOperation(
        "INSERT into nwsbot_social_log(medium, source, message, "
        "response, response_code, resource_uri) values (%s,%s,%s,%s,%s,%s)",
        (
            "facebook",
            myjid,
            message,
            err.value.response,
            err.value.status,
            fbpage,
        ),
    )
    df.addErrback(log.err)


def fbsuccess(response, bot, room, myjid, message):
    """ Got a response from facebook! """
    d = json.loads(response)
    (pageid, postid) = d["id"].split("_")
    url = "http://www.facebook.com/permalink.php?story_fbid=%s&id=%s" % (
        postid,
        pageid,
    )
    html = 'Posted Facebook Message! View <a href="%s">here</a>' % (
        url.replace("&", "&amp;"),
    )
    plain = "Posted Facebook Message! %s" % (url,)
    if room is not None:
        bot.send_groupchat(room, plain, html)

    # Log this
    df = bot.dbpool.runOperation(
        "INSERT into nwsbot_social_log(medium, source, resource_uri, "
        "message, response, response_code) values (%s,%s,%s,%s,%s,%s)",
        ("facebook", myjid, url, message, response, 200),
    )
    df.addErrback(log.err)


def load_chatrooms_from_db(txn, bot, always_join):
    """ Load database configuration and do work

    Args:
      txn (dbtransaction): database cursor
      bot (basicbot): the running bot instance
      always_join (boolean): do we force joining each room, regardless
    """
    # Load up the channel keys
    txn.execute(
        f"SELECT id, channel_key from {bot.name}_channels "
        "WHERE id is not null and channel_key is not null"
    )
    for row in txn.fetchall():
        bot.channelkeys[row["channel_key"]] = row["id"]

    # Load up the routingtable for bot products
    rt = {}
    txn.execute(
        f"SELECT roomname, channel from {bot.name}_room_subscriptions "
        "WHERE roomname is not null and channel is not null"
    )
    rooms = []
    for row in txn.fetchall():
        rm = row["roomname"]
        channel = row["channel"]
        if channel not in rt:
            rt[channel] = []
        rt[channel].append(rm)
        if rm not in rooms:
            rooms.append(rm)
    bot.routingtable = rt
    log.msg(
        ("... loaded %s channel subscriptions for %s rooms")
        % (txn.rowcount, len(rooms))
    )

    # Now we need to load up the syndication
    synd = {}
    txn.execute(
        f"SELECT roomname, endpoint from {bot.name}_room_syndications "
        "WHERE roomname is not null and endpoint is not null"
    )
    for row in txn.fetchall():
        rm = row["roomname"]
        endpoint = row["endpoint"]
        if rm not in synd:
            synd[rm] = []
        synd[rm].append(endpoint)
    bot.syndication = synd
    log.msg(
        ("... loaded %s room syndications for %s rooms")
        % (txn.rowcount, len(synd))
    )

    # Load up a list of chatrooms
    txn.execute(
        f"SELECT roomname, fbpage, twitter from {bot.name}_rooms "
        "WHERE roomname is not null ORDER by roomname ASC"
    )
    oldrooms = list(bot.rooms.keys())
    joined = 0
    for i, row in enumerate(txn.fetchall()):
        rm = row["roomname"]
        # Setup Room Config Dictionary
        if rm not in bot.rooms:
            bot.rooms[rm] = {
                "fbpage": None,
                "twitter": None,
                "occupants": {},
                "joined": False,
            }
        bot.rooms[rm]["fbpage"] = row["fbpage"]
        bot.rooms[rm]["twitter"] = row["twitter"]

        if always_join or rm not in oldrooms:
            presence = domish.Element(("jabber:client", "presence"))
            presence["to"] = "%s@%s/%s" % (rm, bot.conference, bot.myjid.user)
            # Some jitter to prevent overloading
            jitter = 0 if rm in ['botstalk', ] else i % 30
            reactor.callLater(jitter, bot.xmlstream.send, presence)
            joined += 1
        if rm in oldrooms:
            oldrooms.remove(rm)

    # Check old rooms for any rooms we need to vacate!
    for rm in oldrooms:
        presence = domish.Element(("jabber:client", "presence"))
        presence["to"] = "%s@%s/%s" % (rm, bot.conference, bot.myjid.user)
        presence["type"] = "unavailable"
        bot.xmlstream.send(presence)

        del bot.rooms[rm]
    log.msg(
        ("... loaded %s chatrooms, joined %s of them, left %s of them")
        % (txn.rowcount, joined, len(oldrooms))
    )


def load_webhooks_from_db(txn, bot):
    """ Load twitter config from database """
    txn.execute(
        f"SELECT channel, url from {bot.name}_webhooks "
        "WHERE channel is not null and url is not null"
    )
    table = {}
    for row in txn.fetchall():
        url = row["url"]
        channel = row["channel"]
        if url == "" or channel == "":
            continue
        res = table.setdefault(channel, [])
        res.append(url)
    bot.webhooks_routingtable = table
    log.msg("load_webhooks_from_db(): %s subs found" % (txn.rowcount,))


def load_twitter_from_db(txn, bot):
    """ Load twitter config from database """
    txn.execute(
        f"SELECT user_id, channel from {bot.name}_twitter_subs "
        "WHERE user_id is not null and channel is not null"
    )
    twrt = {}
    for row in txn.fetchall():
        user_id = row["user_id"]
        channel = row["channel"]
        d = twrt.setdefault(channel, [])
        d.append(user_id)
    bot.tw_routingtable = twrt
    log.msg("load_twitter_from_db(): %s subs found" % (txn.rowcount,))

    twusers = {}
    txn.execute(
        "SELECT user_id, access_token, access_token_secret, screen_name from "
        f"{bot.name}_twitter_oauth WHERE access_token is not null and "
        "access_token_secret is not null and user_id is not null and "
        "screen_name is not null"
    )
    for row in txn.fetchall():
        user_id = row["user_id"]
        at = row["access_token"]
        ats = row["access_token_secret"]
        twusers[user_id] = {
            "screen_name": row["screen_name"],
            "access_token": oauth.OAuthToken(at, ats),
        }
    bot.tw_users = twusers
    log.msg("load_twitter_from_db(): %s oauth tokens found" % (txn.rowcount,))


def load_facebook_from_db(txn, bot):
    """ Load facebook config from database """
    txn.execute(
        f"SELECT fbpid, channel from {bot.name}_fb_subscriptions "
        "WHERE fbpid is not null and channel is not null"
    )
    fbrt = {}
    for row in txn.fetchall():
        page = row["fbpid"]
        channel = row["channel"]
        if channel not in fbrt:
            fbrt[channel] = []
        fbrt[channel].append(page)
    bot.fb_routingtable = fbrt

    txn.execute(
        f"SELECT fbpid, access_token from {bot.name}_fb_access_tokens "
        "WHERE fbpid is not null and access_token is not null"
    )

    for row in txn.fetchall():
        page = row["fbpid"]
        at = row["access_token"]
        bot.fb_access_tokens[page] = at


def load_chatlog(bot):
    """load up our pickled chatlog"""
    if not os.path.isfile(bot.PICKLEFILE):
        return
    try:
        oldlog = pickle.load(open(bot.PICKLEFILE, "rb"))
        for rm in oldlog:
            bot.chatlog[rm] = oldlog[rm]
            seq = bot.chatlog[rm][-1].seqnum
            if seq is not None and int(seq) > bot.seqnum:
                bot.seqnum = int(seq)
        log.msg(
            "Loaded CHATLOG pickle: %s, seqnum: %s"
            % (bot.PICKLEFILE, bot.seqnum)
        )
    except Exception as exp:
        log.err(exp)


def safe_twitter_text(text):
    """ Attempt to rip apart a message that is too long!
    To be safe, the URL is counted as 24 chars
    """
    # XMPP payload will have entities, unescape those before tweeting
    text = unescape(text)
    # Convert two or more spaces into one
    text = " ".join(text.split())
    # If we are already below TWEET_CHARS, we don't have any more work to do...
    if len(text) < TWEET_CHARS and text.find("http") == -1:
        return text
    chars = 0
    words = text.split()
    # URLs only count as 25 chars, so implement better accounting
    for word in words:
        if word.startswith("http"):
            chars += 25
        else:
            chars += len(word) + 1
    if chars < TWEET_CHARS:
        return text
    urls = re.findall(r"https?://[^\s]+", text)
    if len(urls) == 1:
        text2 = text.replace(urls[0], "")
        sections = re.findall("(.*) for (.*)( till [0-9A-Z].*)", text2)
        if len(sections) == 1:
            text = "%s%s%s" % (sections[0][0], sections[0][2], urls[0])
            if len(text) > TWEET_CHARS:
                sz = TWEET_CHARS - 26 - len(sections[0][2])
                text = "%s%s%s" % (
                    sections[0][0][:sz],
                    sections[0][2],
                    urls[0],
                )
            return text
        if len(text) > TWEET_CHARS:
            # 25 for URL, three dots and space for 29
            return "%s... %s" % (text2[: (TWEET_CHARS - 29)], urls[0])
    if chars > TWEET_CHARS:
        if words[-1].startswith("http"):
            i = -2
            while len(" ".join(words[:i])) > (TWEET_CHARS - 3 - 25):
                i -= 1
            return " ".join(words[:i]) + "... " + words[-1]
    return text[:TWEET_CHARS]


def html_encode(s):
    """Convert stuff in nws text to entities"""
    htmlCodes = (
        ("'", "&#39;"),
        ('"', "&quot;"),
        (">", "&gt;"),
        ("<", "&lt;"),
        ("&", "&amp;"),
    )
    for code in htmlCodes:
        s = s.replace(code[0], code[1])
    return s


def htmlentities(text):
    """Escape chars in the text for HTML presentation

    Args:
      text (str): subject to replace

    Returns:
      str : result of replacement
    """
    for lookfor, replacewith in [
        ("&", "&amp;"),
        (">", "&gt;"),
        ("<", "&lt;"),
        ("'", "&#39;"),
        ('"', "&quot;"),
    ]:
        text = text.replace(lookfor, replacewith)
    return text


def remove_control_characters(html):
    """Get rid of cruft?"""
    # https://github.com/html5lib/html5lib-python/issues/96
    html = re.sub(u"[\x00-\x08\x0b\x0e-\x1f\x7f]", "", html)
    return html


def add_entry_to_rss(entry, rss):
    """Convert a txt Jabber room message to a RSS feed entry

    Args:
      entry(iembot.basicbot.CHAT_LOG_ENTRY): entry

    Returns:
      PyRSSGen.RSSItem
    """
    ts = datetime.datetime.strptime(entry.timestamp, "%Y%m%d%H%M%S")
    txt = entry.txtlog
    m = re.search(r"https?://", txt)
    urlpos = -1
    if m:
        urlpos = m.start()
    else:
        txt += "  "
    ltxt = txt[urlpos:].replace("&amp;", "&").strip()
    if ltxt == "":
        ltxt = "https://mesonet.agron.iastate.edu/projects/iembot/"
    fe = rss.add_entry(order="append")
    fe.title(txt[:urlpos].strip())
    fe.link(link=dict(href=ltxt))
    txt = remove_control_characters(entry.product_text)
    fe.content("<pre>%s</pre>" % (htmlentities(txt),), type="CDATA")
    fe.pubDate(ts.strftime("%a, %d %b %Y %H:%M:%S GMT"))


def daily_timestamp(bot):
    """ Send a timestamp to each room we are in.

    Args:
      bot (iembot.basicbot) instance
    """
    # Make sure we are a bit into the future!
    utc0z = utc() + datetime.timedelta(hours=1)
    utc0z = utc0z.replace(hour=0, minute=0, second=0, microsecond=0)
    mess = "------ %s [UTC] ------" % (utc0z.strftime("%b %-d, %Y"),)
    for rm in bot.rooms:
        bot.send_groupchat(rm, mess)

    tnext = utc0z + datetime.timedelta(hours=24)
    delta = (tnext - utc()).total_seconds()
    log.msg(f"Calling daily_timestamp in {delta:.2f} seconds")
    return reactor.callLater(delta, daily_timestamp, bot)
