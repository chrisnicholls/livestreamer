import re
import requests
from livestreamer.exceptions import PluginError
from livestreamer.options import Options
from livestreamer.plugin import Plugin
from livestreamer.utils import urlopen, parse_xml
from livestreamer.stream import HLSStream, HTTPStream
import random
import hashlib
from uuid import getnode as get_mac
import urllib

LOGIN_URL = "https://id.s.nfl.com/login"
BASE_URL = "https://gamepass.nfl.com/nflgp"
LOGIN_ERROR_REDIRECT = '/secure/login?redirect=loginform&redirectnosub=packages&redirectsub=schedule'
LOGIN_SUCCESS_REDIRECT = '/secure/login?redirect=loginform&redirectnosub=packages&redirectsub=schedule'
GAMES_PATH = '/servlets/games'
ENCRYPT_VIDEO_PATH = "/servlets/encryptvideopath"
PUBLISH_POINT_PATH = "/servlets/publishpoint"

bitrates = ['4500', '3000', '2400', '1600', '1200', '800', '400']

def gen_plid():
    # MD5's Random string. this approach was taken from the xbmc-gamepass project
    # at https://github.com/Alexqw/xbmc-gamepass
    rand = random.getrandbits(10)
    mac_address = str(get_mac())
    m = hashlib.md5(str(rand) + mac_address)
    return m.hexdigest()


def parse_game_alias(url):
    p = re.compile(".*gamepass.nfl.com/nflgp/console.jsp\?eid=([0-9]+)")
    m = p.match(url)

    if m is None:
        raise PluginError("Could not parse game alias! "
                          "Url should be in format: http://gamepass.nfl.com/nflgp/console.jsp?eid=1234")

    return m.group(1)


class GamepassAPI(object):

    def __init__(self):
        self.session = requests.session()

    def login(self, username, password):
        data = {
            'username': username,
            'password': password,
            'vendor_id': 'nflptnrnln',
            'error_url': BASE_URL + LOGIN_ERROR_REDIRECT,
            'success_url': BASE_URL + LOGIN_SUCCESS_REDIRECT
        }

        response = urlopen(LOGIN_URL, method="post", data=data, session=self.session)

        print "Login Response:\n%s" % str(response)

    def set_cookies(self, cookies):
        for cookie in cookies.split(";"):
            try:
                name, value = cookie.split("=")
            except ValueError:
                continue

            self.session.cookies[name.strip()] = value.strip()

    def get_games_for_week(self, game_alias):
        data = {
            'isFlex': True,
            'eid': game_alias
        }

        games_url = BASE_URL + GAMES_PATH
        response = urlopen(games_url, method="post", data=data, session=self.session)

        return response.content

    def get_encrypted_video_path(self, program_id):
        data = {
            'path': program_id,
            'plid': gen_plid(),
            'type': 'fgpa',
            'isFlex': 'true'
        }

        url = BASE_URL.replace("https", "http") + ENCRYPT_VIDEO_PATH

        response = urlopen(url, method="post", data=data, session=self.session)
        return response.content

    def publish_point(self, game_id):
        url = BASE_URL.replace("https", "http") + PUBLISH_POINT_PATH

        data = {
            'id': game_id,
            'type': "game",
            'nt': "1",
            'gt': "live"
        }

        headers = {
            'user-agent': "Android"
        }

        response = urlopen(url, method="post", data=data, session=self.session, headers=headers)
        return response.content

    def play(self, game_path):
        #Doesn't actually play anything but does the 'play' call which returns the stream urls

        #game_path is in the format
        url, port, path = game_path.partition(':443')
        path = path.replace('?', '&')
        url = url.replace('adaptive://', 'http://') + port + '/play?' + urllib.quote_plus('url=' + path, ':&=')

        response = urlopen(url, session=self.session)
        return response.content

    def get_stream_auth_cookie(self, url):
        response = urlopen(url, method="get", session=self.session)

        return response.headers['Set-Cookie']


class Gamepass(Plugin):
    options = Options()

    @classmethod
    def can_handle_url(cls, url):
        return "gamepass.nfl.com" in url

    def __init__(self, url):
        Plugin.__init__(self, url)

        self.api = GamepassAPI()

        self.game_alias = parse_game_alias(url)

    def authenticate(self):
        cookies = self.options.get("cookies")
        username = self.options.get("username")
        password = self.options.get("password")

        if cookies is not None:
            self.api.set_cookies(cookies)
        elif username is not None and password is not None:
            self.api.login(username, password)
        else:
            raise PluginError("Must specify --gamepass-cookies "
                              "or --gamepass-username and --gamepass-password")

    def parse_streams(self, streams_root):
        streams = {}
        for stream in streams_root.find("streamDatas").findall("streamData"):
            path = stream.attrib["url"]

            p = re.compile(".*_([0-9]+)(.mp4)?$")
            bitrate = p.match(path).group(1)

            httpserver = stream.find("httpservers").find("httpserver")
            server_name = httpserver.attrib["name"]
            server_port = httpserver.attrib["port"]

            url = "http://" + server_name + ":" + server_port + path + ".m3u8"

            self.session.cookies = self.api.session.cookies
            streams[bitrate] = HLSStream(self.session, url)
        return streams

    def get_vod_streams(self, program_id):
        root = parse_xml(self.api.get_encrypted_video_path(program_id))
        game_path = root.find("path").text

        root = parse_xml(self.api.play(game_path))

        return self.parse_streams(root)

    def get_live_streams(self, game_id):
        root = parse_xml(self.api.publish_point(game_id))

        url = root.find("path").text
        url = url.replace("adaptive://", "http://")

        cookie = self.api.get_stream_auth_cookie(url)

        headers = {
            'Cookie': cookie
        }

        streams = {}
        for bitrate in bitrates:
            streams[bitrate] = HLSStream(self.session, url.replace("androidtab", bitrate), headers=headers)

        return streams

    def _get_streams(self):
        self.authenticate()
        streams = {}

        root = parse_xml(self.api.get_games_for_week(self.game_alias))
        games = root.find("games")

        game = None
        for game in games.findall('game'):
            if game.find('elias').text == self.game_alias:
                break

        if game is None:
            PluginError("Could not find game %s" %self.game_alias)

        is_live = game.find("isLive")
        if is_live is not None and is_live.text == "true":
            game_id = game.find("id").text
            streams = self.get_live_streams(game_id)
        else:
            program_id = game.find("programId").text
            streams = self.get_vod_streams(program_id)

        return streams

__plugin__ = Gamepass