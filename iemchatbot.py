# Copyright (c) 2005 Iowa State University
# http://mesonet.agron.iastate.edu/ -- mailto:akrherz@iastate.edu
#
# This program is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc.,
# 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.
""" Chat bot implementation for iemchat """

__revision__ = '$Id: iemchatbot.py 3160 2008-04-09 23:31:06Z akrherz $'


from twisted.words.protocols.jabber import client, jid
from twisted.words.xish import domish, xpath
from twisted.web import xmlrpc, client
from twisted.python import log
from twisted.enterprise import adbapi
from twisted.words.xish.xmlstream import STREAM_END_EVENT
from twisted.internet.task import LoopingCall

import mx.DateTime, socket, re
import StringIO, traceback, smtplib, base64, urllib
from email.MIMEText import MIMEText

import secret

CHATLOG = {}
ROSTER = {}

CWSU = ['zabchat', 'ztlchat', 'zbwchat', 'zauchat', 'zobchat', 
        'zdvchat', 'zfwchat', 'zhuchat', 'zidchat', 'zkcchat', 
        'zjxchat', 'zlachat', 'zmechat', 'zmachat', 'zmpchat', 
        'znychat', 'zoachat', 'zlcchat', 'zsechat', 'zdcchat']

PRIVATE_ROOMS = ['rgn3fwxchat', 'broemchat', 'wrhchat', 'abqemachat',
                 'jaxemachat', 'bmxalert', 'mlbemchat', 'wxiaweather',
                 'kccichat', 'vipir6and7', 'abc3340', 'dmxemchat',
                 'janhydrochat', 'bmxemachat', 'fwdemachat', 'tbwemchat',
                 'tbwnetchat']

PUBLIC_ROOMS = ['botstalk', 'peopletalk']

WFOS = ['abqchat', 'afcchat', 'afgchat', 'ajkchat', 'akqchat', 'alychat',
        'amachat', 'bgmchat', 'bmxchat', 'boichat', 'bouchat', 'boxchat',
        'brochat', 'btvchat', 'bufchat', 'byzchat', 'caechat', 'carchat',
        'chschat', 'crpchat', 'ctpchat', 'cyschat', 'ekachat', 'epzchat',
        'ewxchat', 'keychat', 'ffcchat', 'fgzchat', 'fwdchat', 'ggwchat',
        'gjtchat', 'gspchat', 'gyxchat', 'hfochat', 'hgxchat', 'hnxchat',
        'hunchat', 'ilmchat', 'janchat', 'jaxchat', 'jklchat', 'lchchat',
        'lixchat', 'lknchat', 'lmkchat', 'loxchat', 'lubchat', 'lwxchat',
        'lzkchat', 'mafchat', 'megchat', 'mflchat', 'mfrchat', 'mhxchat',
        'mlbchat', 'mobchat', 'mrxchat', 'msochat', 'mtrchat', 'ohxchat',
        'okxchat', 'otxchat', 'ounchat', 'pahchat', 'pbzchat', 'pdtchat',
        'phichat', 'pihchat', 'pqrchat', 'psrchat', 'pubchat', 'rahchat',
        'revchat', 'riwchat', 'rlxchat', 'rnkchat', 'sewchat', 'sgxchat',
        'shvchat', 'sjtchat', 'sjuchat', 'slcchat', 'stochat', 'taechat',
        'tbwchat', 'tfxchat', 'tsachat', 'twcchat', 'vefchat', 'abrchat',
        'apxchat', 'arxchat', 'bischat', 'clechat', 'ddcchat', 'dlhchat',
        'dtxchat', 'dvnchat', 'eaxchat', 'fgfchat', 'fsdchat', 'gidchat',
        'gldchat', 'grbchat', 'grrchat', 'ictchat', 'ilnchat', 'ilxchat',
        'indchat', 'iwxchat', 'lbfchat', 'lotchat', 'lsxchat', 'mkxchat',
        'mpxchat', 'mqtchat', 'oaxchat', 'sgfchat', 'topchat', 'unrchat',
        'dmxchat', 'gumchat']

PHONE_RE = re.compile(r'(\d{3})\D*(\d{3})\D*(\d{4})\D*(\d*)')

DBPOOL = adbapi.ConnectionPool("psycopg2",  database="openfire")

MAIL_COUNT = 10

class IEMChatXMLRPC(xmlrpc.XMLRPC):

    def xmlrpc_getUpdate(self, room, seqnum):
        """ Return most recent messages since timestamp (ticks...) """
        #fts = float(timestamp) / 10
     
        #print "XMLRPC-request", room, seqnum, CHATLOG[room]['seqnum']
        r = []
        if (not CHATLOG.has_key(room)):
            return r
        for k in range(len(CHATLOG[room]['seqnum'])):
            if (CHATLOG[room]['seqnum'][k] > seqnum):
                ts = mx.DateTime.DateTimeFromTicks( 
                     CHATLOG[room]['timestamps'][k] / 100.0)
                r.append( [ CHATLOG[room]['seqnum'][k] , 
                            ts.strftime("%Y%m%d%H%M%S"), 
                            CHATLOG[room]['author'][k], 
                            CHATLOG[room]['log'][k] ] )
        #print r
        return r


class JabberClient:
    xmlstream = None

    def __init__(self, myJid):
        self.myJid = myJid
        self.seqnum = 0



    def send_presence(self):
        presence = domish.Element(('jabber:client','presence'))
        presence.addElement('status').addContent('Online')
        self.xmlstream.send(presence)

        socket.setdefaulttimeout(60)

    def keepalive(self):
        if (self.xmlstream is not None):
            self.xmlstream.send(' ')


    def rawDataInFn(self, data):
        print 'RECV', unicode(data,'utf-8','ignore').encode('ascii', 'replace')
    def rawDataOutFn(self, data):
        if (data == ' '):
            return
        print 'SEND', unicode(data,'utf-8','ignore').encode('ascii', 'replace')

    def authd(self, xmlstream):
        print "Logged into Jabber Chat Server!"
        self.xmlstream = xmlstream
        self.xmlstream.rawDataInFn = self.rawDataInFn
        self.xmlstream.rawDataOutFn = self.rawDataOutFn

        self.xmlstream.addObserver('/message',  self.processor)
        self.xmlstream.addObserver('/presence/x/item',  self.presence_processor)


        self.send_presence()
        self.join_chatrooms()
        lc = LoopingCall(self.keepalive)
        lc.start(60)
        self.xmlstream.addObserver(STREAM_END_EVENT, lambda _: lc.stop())

    def join_chatrooms(self):
        for rm in CWSU + PRIVATE_ROOMS + PUBLIC_ROOMS + WFOS:
            ROSTER[rm] = {}
            presence = domish.Element(('jabber:client','presence'))
            presence['to'] = "%s@conference.%s/iembot" % (rm, secret.CHATSERVER)
            self.xmlstream.send(presence)


    def presence_processor(self, elem):
        """ Process presence items
        <presence to="iembot@localhost/twisted_words" 
                  from="gumchat@conference.localhost/iembot">
         <x xmlns="http://jabber.org/protocol/muc#user">
          <item jid="iembot@localhost/twisted_words" affiliation="none" 
                role="participant"/>
         </x>
        </presence>
        """
        _room = jid.JID( elem["from"] ).user
        _handle = jid.JID( elem["from"] ).resource
        items = xpath.queryForNodes('/presence/x/item', elem)
        if (items is None):
            return
        for item in items:
            if (item.attributes.has_key('jid') and
                item.attributes.has_key('affiliation') and 
                item.attributes.has_key('role') ):
                if (item.attributes['role'] == "none"):
                    if ( ROSTER[_room].has_key(_handle) ):
                        del( ROSTER[_room][_handle] )
                else:
                    ROSTER[ _room ][ _handle ] = {
                      'jid': item.attributes['jid'],
                      'affiliation': item.attributes['affiliation'],
                      'role': item.attributes['role'] }

    def failure(self, f):
        print f

    def debug(self, elem):
        print elem.toXml().encode('utf-8')
        print "="*20

    def nextSeqnum(self,):
        self.seqnum += 1
        return self.seqnum

    def processMessageGC(self, elem):
        _from = jid.JID( elem["from"] )
        room = _from.user
        res = _from.resource
        if (res is None): res = "---"
        if (not CHATLOG.has_key(room)):
            CHATLOG[room] = {'seqnum': [-1]*40, 'timestamps': [0]*40, 
                             'log': ['']*40, 'author': ['']*40}
        ticks = int(mx.DateTime.gmt().ticks() * 100)
        x = xpath.queryForNodes('/message/x', elem)
        if (x is not None and x[0].hasAttribute("stamp") ):
            xdelay = x[0]['stamp']
            print "FOUND Xdelay", xdelay, ":"
            delayts = mx.DateTime.strptime(xdelay, "%Y%m%dT%H:%M:%S")
            ticks = int(delayts.ticks() * 100)
        elif (x is not None):
            print "What is this?", x[0].toXml()

        CHATLOG[room]['seqnum'] = CHATLOG[room]['seqnum'][1:] + [self.nextSeqnum(),]
        CHATLOG[room]['timestamps'] = CHATLOG[room]['timestamps'][1:] + [ticks,]
        CHATLOG[room]['author'] = CHATLOG[room]['author'][1:] + [res,]

        html = xpath.queryForNodes('/message/html/body', elem)
        if (html != None):
            CHATLOG[room]['log'] = CHATLOG[room]['log'][1:] + [html[0].toXml(),]
        else:
            try:
                body = xpath.queryForString('/message/body', elem)
                CHATLOG[room]['log'] = CHATLOG[room]['log'][1:] + [body,]
            except:
                print room, 'VERY VERY BAD'

        # If the message is x-delay, old message, no relay
        if (x is not None):
            return
        bstring = xpath.queryForString('/message/body', elem)

        # Send a copy of the message to the peopletalk room
        # TODO: support sending the HTML variant
        if (res != "iembot" and room in WFOS):
            self.send_groupchat("peopletalk", "[%s] %s: %s"%(room,res,bstring))

        # Look for bot commands
        if (res != "iembot") and re.match(r"^iembot:", bstring):
            self.process_groupchat_cmd(room, res, bstring[7:].strip())

        # Look for legacy ping
        if (res != "iembot") and re.match(r"^ping", bstring):
            self.process_groupchat_cmd(room, res, "ping")

    def process_groupchat_cmd(self, room, res, cmd):
        """ I actually process the groupchat commands and do stuff """

        # Look for sms request
        if re.match(r"^sms", cmd):
            # Make sure the user is an owner or admin, I think
            aff = None
            if (ROSTER[room].has_key(res)):
                aff = ROSTER[room][res]['affiliation']
            if (aff in ['owner','admin']):
                self.process_sms(room, cmd[3:], ROSTER[room][res]['jid'])
            else:
                err = "%s: Sorry, you must be a room admin to send a SMS" \
                       % (res,)
                self.send_groupchat(room, err)

        # Look for users request
        elif re.match(r"^users", cmd):
            rmess = ""
            for hndle in ROSTER[room].keys():
                rmess += "%s (%s), " % (hndle, ROSTER[room][hndle]['jid'],)
            self.send_groupchat(room, "JIDs in room: %s" % (rmess,))

        # Look for users request
        elif re.match(r"^ping", cmd):
            self.send_groupchat(room, "%s: %s"%(res, "pong"))

        # Else send error message about what iembot support
        else:
            err = """Unsupported command: '%s'
Current Supported Commands:
  iembot: sms My SMS Message to send  ### Send SMS Message to this Group
  iembot: ping          ### Test connectivity with a 'pong' response
  iembot: users         ### Generates list of users in room""" % (cmd,)
            self.send_groupchat(room, err)

    def process_sms(self, room, send_txt, sender):
        # Query for users in chatgroup
        sql = "select i.propvalue as num, i.username as username from \
         iemchat_userprop i, jivegroupuser j WHERE \
         i.username = j.username and \
         j.groupname = '%sgroup'" % (room[:3].lower(),)
        DBPOOL.runQuery(sql).addCallback(self.sendSMS, room, send_txt, sender)

    def sendSMS(self, l, rm, send_txt, sender):
        """ https://mobile.wrh.noaa.gov/mobile_secure/quios_relay.php 
         numbers - string, a comma delimited list of 10 digit
                   phone numbers
         message - string, the message you want to send
        """
        if l:
            numbers = []
            for i in range(len(l)):
                numbers.append( l[i][0] )
                #username = l[i][1]
            url = "https://mobile.wrh.noaa.gov/mobile_secure/quios_relay.php"
            basicAuth = base64.encodestring("%s:%s" % (secret.QUIOS_USER, 
                                            secret.QUIOS_PASS) )
            authHeader = "Basic " + basicAuth.strip()
            print 'Sender is', sender
            payload = urllib.urlencode({'numbers': ",".join(numbers),\
                                        'sender': sender,\
                                          'message': send_txt})
            client.getPage(url, postdata=payload, method="POST",\
              headers={"Authorization": authHeader,\
                       "Content-type":"application/x-www-form-urlencoded"}\
              ).addCallback(\
              self.sms_success, rm).addErrback(self.sms_failure, rm)
        else:
            self.send_groupchat(rm, "No SMS numbers found for chatgroup.")

    def sms_failure(self, res, rm):
        print res
        message = domish.Element(('jabber:client','message'))
        message['to'] = "%s@conference.%s" %(rm, secret.CHATSERVER)
        message['type'] = "groupchat"
        message.addElement('body',None,"SMS Send Failure, Sorry")
        self.xmlstream.send(message)

    def sms_success(self, res, rm):
        message = domish.Element(('jabber:client','message'))
        message['to'] = "%s@conference.%s" %(rm, secret.CHATSERVER)
        message['type'] = "groupchat"
        message.addElement('body',None,"Sent SMS")
        self.xmlstream.send(message)

    def processor(self, elem):
        try:
            self.processMessage(elem)
        except:
            MAIL_COUNT -= 1
            if MAIL_COUNT < 0:
                print "LIMIT MAIL_COUNT"
                return
            io = StringIO.StringIO()
            traceback.print_exc(file=io)
            print io.getvalue() 
            msg = MIMEText("%s\n\n%s\n\n"%(elem.toXml(), io.getvalue() ))
            msg['subject'] = 'iembot Traceback'
            msg['From'] = "ldm@mesonet.agron.iastate.edu"
            msg['To'] = "akrherz@iastate.edu"

            s = smtplib.SMTP()
            s.connect()
            s.sendmail(msg["From"], msg["To"], msg.as_string())
            s.close()


    def processMessage(self, elem):
        t = ""
        try:
            t = elem["type"]
        except:
            print elem.toXml(), 'BOOOOOOO'

        bstring = xpath.queryForString('/message/body', elem)
        if (bstring == ""):
            return

        if (t == "groupchat"):
            self.processMessageGC(elem)

        elif (t == "chat" or t == ""):
            self.processMessagePC(elem)

    def talkWithUser(self, elem):
        _from = jid.JID( elem["from"] )
        bstring = xpath.queryForString('/message/body', elem)
        if (bstring is None):
            print "Empty conversation?", elem.toXml()
            return 

        bstring = bstring.lower()
        if re.match(r"^set sms#", bstring):
            self.handle_sms_request( elem, bstring)
        else:
            self.send_help_message( elem["from"] )

    def handle_sms_request(self, elem, bstring):
        _from = jid.JID( elem["from"] )
        cmd = bstring.replace("set sms#", "").strip()

        # They can opt out, if they wish
        if (cmd == "0" or cmd == ""):
            sql = "DELETE from iemchat_userprop WHERE username = '%s' and \
               name = 'sms#'" % (_from.user, )
            DBPOOL.runOperation( sql )
            msg = "Thanks, SMS service disabled for your account"
            self.send_privatechat(elem["from"], msg)
            return
        ttt = PHONE_RE.search(cmd)
        if ttt is None:
            self.send_help_message( elem["from"] )
            return
        ar = ttt.groups()
        if len(ar) < 4:
            self.send_help_message( elem["from"] )
            return
        clean_number = "%s%s%s" % (ar[0], ar[1], ar[2])
        clean_number2 = "%s-%s-%s" % (ar[0], ar[1], ar[2])
        sql = "DELETE from iemchat_userprop WHERE username = '%s' and \
               name = 'sms#'" % (_from.user, )
        DBPOOL.runOperation( sql )
        sql = "INSERT into iemchat_userprop(username, name, propvalue)\
               VALUES ('%s','%s','%s')" % \
               (_from.user, 'sms#', clean_number)
        DBPOOL.runOperation( sql )
        msg = """Thanks, SMS updated to: %s
Please note: This service is provided without warranty and standard text messaging rates apply.""" % (clean_number2,)
        self.send_privatechat(elem["from"], msg)


    def send_help_message(self, to):
        msg = """Hi, I am iembot.  You can try talking directly with me.
Currently supported commands are:
  set sms# 555-555-5555  (command will set your SMS number)
  set sms# 0             (disables SMS messages from iemchat)"""
        self.send_privatechat(to, msg)

    def send_privatechat(self, to, mess):
        message = domish.Element(('jabber:client','message'))
        message['to'] = to
        message['type'] = "chat"
        message.addElement('body',None, mess)
        self.xmlstream.send(message)

    def send_groupchat(self, room, mess):
        message = domish.Element(('jabber:client','message'))
        message['to'] = "%s@conference.%s" %(room, secret.CHATSERVER)
        message['type'] = "groupchat"
        message.addElement('body',None, mess)
        self.xmlstream.send(message)

    def send_private_request(self, myjid):
        # Got a private message via MUC, send error and then private message
        _handle = myjid.resource
        _room = myjid.user
        if (not ROSTER[_room].has_key(_handle)):
            return
        realjid = ROSTER[_room][_handle]["jid"]

        self.send_help_message( realjid )

        message = domish.Element(('jabber:client','message'))
        message['to'] = myjid.full()
        message['type'] = "chat"
        message.addElement('body',None,"I can't help you here, please chat \
with me outside of a groupchat.  I have initated such a chat for you.")
        self.xmlstream.send(message)

    def processMessagePC(self, elem):
        _from = jid.JID( elem["from"] )
        # Intercept private messages via a chatroom, can't do that :)
        if (_from.host == "conference.%s" % (secret.CHATSERVER,)):
            self.send_private_request( _from )
            return

        if (_from.userhost() != "iembot_ingest@%s" % (secret.CHATSERVER,) ):
            self.talkWithUser(elem)
            return

        # Go look for body to see routing info! 
        # Get the body string
        bstring = xpath.queryForString('/message/body', elem)
        htmlstr = xpath.queryForString('/message/html/body', elem)
        if (len(bstring) < 3):
            print "BAD!!!"
            return
        wfo = bstring[:3]
        # Look for HTML
        html = xpath.queryForNodes('/message/html', elem)

        # Route message to botstalk room in tact
        message = domish.Element(('jabber:client','message'))
        message['to'] = "botstalk@conference.%s" % (secret.CHATSERVER,)
        message['type'] = "groupchat"
        message.addChild( elem.body )
        if (elem.html):
            message.addChild(elem.html)
        self.xmlstream.send(message)

        # Send to chatroom, clip body
        message = domish.Element(('jabber:client','message'))
        message['to'] = "%schat@conference.%s" % (wfo.lower(), secret.CHATSERVER,)
        message['type'] = "groupchat"
        message.addElement('body',None,bstring[4:])
        if (elem.html):
            message.addChild(elem.html)

        self.xmlstream.send(message)
        if (wfo.upper() == "TBW"):
            message['to'] = "%snetchat@conference.%s" % (wfo.lower(), secret.CHATSERVER)
            self.xmlstream.send(message)
        if (wfo.upper() == "TBW" or wfo.upper() == "MLB"):
            message['to'] = "%semchat@conference.%s" % (wfo.lower(), secret.CHATSERVER)
            self.xmlstream.send(message)
        if (wfo.upper() == "BMX" or wfo.upper() == "FWD"):
            message['to'] = "%semachat@conference.%s" % (wfo.lower(), secret.CHATSERVER)
            self.xmlstream.send(message)
        if (wfo.upper() == "BMX" or wfo.upper() == "HUN"):
            message['to'] = "abc3340@conference.%s" % ( secret.CHATSERVER,)
            self.xmlstream.send(message)
        if (wfo.upper() == "BMX"):
            message['to'] = "bmxalert@conference.%s" % ( secret.CHATSERVER,)
            self.xmlstream.send(message)
        if (wfo.upper() == "MOB" or wfo.upper() == "TAE" or wfo.upper() == "BMX"):
            message['to'] = "vipir6and7@conference.%s" % ( secret.CHATSERVER,)
            self.xmlstream.send(message)
        if (wfo.upper() == "FFC"):
            message['to'] = "wxiaweather@conference.%s" % ( secret.CHATSERVER,)
            self.xmlstream.send(message)
        if (wfo.upper() == "JAN"):
            message['to'] = "janhydrochat@conference.%s" % ( secret.CHATSERVER,)
            self.xmlstream.send(message)
        if (wfo.upper() == "JAX"):
            message['to'] = "jaxemachat@conference.%s" % (secret.CHATSERVER,)
            self.xmlstream.send(message)
        if (wfo.upper() == "ABQ"):
            message['to'] = "abqemachat@conference.%s" % ( secret.CHATSERVER,)
            self.xmlstream.send(message)
        if (wfo.upper() == "SLC"):
            message['to'] = "wrhchat@conference.%s" % ( secret.CHATSERVER,)
            self.xmlstream.send(message)
        if (wfo.upper() == "BRO"):
            message['to'] = "broemchat@conference.%s" % ( secret.CHATSERVER,)
            self.xmlstream.send(message)
        if (wfo.upper() == "DMX"):
            message['to'] = "%semchat@conference.%s" % (wfo.lower(), secret.CHATSERVER)
            self.xmlstream.send(message)
            message['to'] = "kccichat@conference.%s" % (secret.CHATSERVER,)
            self.xmlstream.send(message)
        

