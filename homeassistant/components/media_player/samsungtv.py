"""
Support for interface with an Samsung TV.

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/media_player.samsungtv/
"""
import asyncio
from datetime import timedelta
import logging
import socket
import requests

import voluptuous as vol

from homeassistant.components.media_player import (
    MediaPlayerDevice, PLATFORM_SCHEMA)
from homeassistant.components.media_player.const import (
    MEDIA_TYPE_CHANNEL, SUPPORT_NEXT_TRACK, SUPPORT_PAUSE,
    SUPPORT_PLAY, SUPPORT_PLAY_MEDIA, SUPPORT_PREVIOUS_TRACK, SUPPORT_TURN_OFF,
    SUPPORT_TURN_ON, SUPPORT_VOLUME_MUTE, SUPPORT_VOLUME_STEP,
    SUPPORT_VOLUME_SET,SUPPORT_SELECT_SOURCE)
from homeassistant.const import (
    CONF_HOST, CONF_MAC, CONF_NAME, CONF_PORT, CONF_TIMEOUT, STATE_OFF,
    STATE_ON)
import homeassistant.helpers.config_validation as cv
from homeassistant.util import dt as dt_util

REQUIREMENTS = ['https://github.com/jgrieger1/samsungctl/archive/myBranch.zip#samsungctl[websocket]==0.8.0b', 'wakeonlan==1.1.6']

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = 'Samsung TV Remote'
DEFAULT_PORT = 55000
DEFAULT_TIMEOUT = 1

CONF_SOURCES = 'sources'
CONF_RIGHT_CLICKS = 'right_clicks'

KEY_PRESS_TIMEOUT = 1.2
KNOWN_DEVICES_KEY = 'samsungtv_known_devices'

SUPPORT_SAMSUNGTV = SUPPORT_PAUSE | SUPPORT_VOLUME_STEP | \
    SUPPORT_VOLUME_MUTE | SUPPORT_PREVIOUS_TRACK | \
    SUPPORT_NEXT_TRACK | SUPPORT_TURN_OFF | SUPPORT_PLAY | SUPPORT_PLAY_MEDIA | \
    SUPPORT_VOLUME_SET | SUPPORT_SELECT_SOURCE

VIDEO_SOURCES_SCHEMA = vol.Schema({
    vol.Required(CONF_NAME): cv.string,
    vol.Required(CONF_RIGHT_CLICKS): cv.positive_int,
})

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_HOST): cv.string,
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
    vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): cv.positive_int,
    vol.Optional(CONF_SOURCES): vol.All(cv.ensure_list, [VIDEO_SOURCES_SCHEMA]),
})


def setup_platform(hass, config, add_entities, discovery_info=None):
    """Set up the Samsung TV platform."""
    known_devices = hass.data.get(KNOWN_DEVICES_KEY)
    if known_devices is None:
        known_devices = set()
        hass.data[KNOWN_DEVICES_KEY] = known_devices

    uuid = None
    sources = None
    # Is this a manual configuration?
    if config.get(CONF_HOST) is not None:
        host = config.get(CONF_HOST)
        port = config.get(CONF_PORT)
        name = config.get(CONF_NAME)
        mac = config.get(CONF_MAC)
        timeout = config.get(CONF_TIMEOUT)
        sources = config.get(CONF_SOURCES)
    elif discovery_info is not None:
        tv_name = discovery_info.get('name')
        model = discovery_info.get('model_name')
        host = discovery_info.get('host')
        name = "{} ({})".format(tv_name, model)
        port = DEFAULT_PORT
        timeout = DEFAULT_TIMEOUT
        mac = None
        udn = discovery_info.get('udn')
        if udn and udn.startswith('uuid:'):
            uuid = udn[len('uuid:'):]
    else:
        _LOGGER.warning("Cannot determine device")
        return

    # Only add a device once, so discovered devices do not override manual
    # config.
    ip_addr = socket.gethostbyname(host)
    if ip_addr not in known_devices:
        known_devices.add(ip_addr)
        add_entities([SamsungTVDevice(host, port, name, timeout, mac, uuid, sources)])
        _LOGGER.info("Samsung TV %s:%d added as '%s'", host, port, name)
    else:
        _LOGGER.info("Ignoring duplicate Samsung TV %s:%d", host, port)


class SamsungTVDevice(MediaPlayerDevice):
    """Representation of a Samsung TV."""

    def __init__(self, host, port, name, timeout, mac, uuid, sources):
        """Initialize the Samsung device."""
        from samsungctl import exceptions
        from samsungctl import Remote
        from samsungctl import Config as samsungctl_config
        import wakeonlan
        # Save a reference to the imported classes
        self._exceptions_class = exceptions
        self._remote_class = Remote
        self._name = name
        self._mac = mac
        self._uuid = uuid
        self._wol = wakeonlan
        # Assume that the TV is not muted
        self._muted = False
        # Assume that the TV is in Play mode
        self._playing = True
        self._state = None
        self._remote = None
        # Mark the end of a shutdown command (need to wait 15 seconds before
        # sending the next command to avoid turning the TV back ON).
        self._end_of_power_off = None
        self._volume = 0
        # Setup the source list
        self._source_list = {}
        if sources is not None:
            for entry in sources:
                self._source_list[entry[CONF_NAME]] = entry.get(CONF_RIGHT_CLICKS)
        # Generate a configuration for the Samsung library
        self._samsungctl_config = samsungctl_config.load('/home/homeassistant/.homeassistant/samsungctl.conf')
        self._config = {
            'name': 'HomeAssistant',
            'description': name,
            'id': 'ha.component.samsung',
            'port': port,
            'host': host,
            'timeout': timeout,
        }

        if self._config['port'] == 8001 or self._config['port'] == 8002:
            self._config['method'] = 'websocket'
        else:
            self._config['method'] = 'legacy'

    def update(self):
        """Update state of device."""
        if self._config['method'] == 'websocket':
            if self._power_off_in_progress():
                self._state = STATE_OFF
            else:
                try:
                    if self.is_tv_on():
                        remote = self.get_remote()
                        if remote is not None:
                            try:
                                self._volume = remote.volume
                            except Exception:
                                _LOGGER.debug("Failed to get volume property from TV.  Is TV turning off?")
                            try:
                                self._muted = remote.mute
                            except Exception:
                                _LOGGER.debug("Failed to get mute property from TV.  Is TV turning off?")
                except OSError:
                    self._state = STATE_OFF
                    self._remote = None
        else:
            self.send_key("KEY")

    def get_remote(self):
        """Create or return a remote control instance."""
        if self._remote is None:
            # We need to create a new instance to reconnect.
            self._remote = self._remote_class(self._samsungctl_config)

        return self._remote

    def send_key(self, key):
        """Send a key to the tv and handles exceptions."""
        if (self._mac and self._state == STATE_ON) or not self._mac:
            if self._power_off_in_progress() \
                    and key not in ('KEY_POWER', 'KEY_POWEROFF'):
                _LOGGER.info("TV is powering off, not sending command: %s", key)
                return
            try:
                # recreate connection if connection was dead
                retry_count = 1
                for _ in range(retry_count + 1):
                    try:
                        self.get_remote().control(key)
                        break
                    except (self._exceptions_class.ConnectionClosed,
                            BrokenPipeError):
                        # BrokenPipe can occur when the commands is sent to fast
                        self._remote = None
                self._state = STATE_ON
            except (self._exceptions_class.UnhandledResponse,
                    self._exceptions_class.AccessDenied):
                # We got a response so it's on.
                self._state = STATE_ON
                self._remote = None
                _LOGGER.warning("Failed sending command %s", key, exc_info=True)
                return
            except OSError:
                self._state = STATE_OFF
                self._remote = None
            if self._power_off_in_progress():
                self._state = STATE_OFF

    def set_property(self, prop, value):
        if self._power_off_in_progress():
            _LOGGER.info("TV is powering off, not setting property: %s", prop)
            return
        try:
            if self.is_tv_on():
                if prop == 'volume':
                    self.get_remote().volume = value
                if prop == 'source':
                    self.get_remote().source = value
                self.update()
        except OSError:
            self._state = STATE_OFF
            self._remote = None
        except ValueError:
            _LOGGER.error("ValueError trying to set property: %s with value of: %s", prop, value)
            pass

    def _power_off_in_progress(self):
        return self._end_of_power_off is not None and \
               self._end_of_power_off > dt_util.utcnow()

    @property
    def unique_id(self) -> str:
        """Return the unique ID of the device."""
        return self._uuid

    @property
    def name(self):
        """Return the name of the device."""
        return self._name

    @property
    def state(self):
        """Return the state of the device."""
        return self._state

    @property
    def is_volume_muted(self):
        """Boolean if volume is currently muted."""
        return self._muted

    @property
    def volume_level(self):
        return float(self._volume) / 100

    @property
    def source_list(self):
        """Return a list of available input sources."""
        sources = []
        for key in self._source_list:
            sources.append(key)
        return sources


    @property
    def supported_features(self):
        """Flag media player features that are supported."""
        if self._mac:
            return SUPPORT_SAMSUNGTV | SUPPORT_TURN_ON
        return SUPPORT_SAMSUNGTV

    def turn_off(self):
        """Turn off media player."""
        self._end_of_power_off = dt_util.utcnow() + timedelta(seconds=15)

        if self._config['method'] == 'websocket':
            self.send_key('KEY_POWER')
        else:
            self.send_key('KEY_POWEROFF')
        # Force closing of remote session to provide instant UI feedback
        try:
            self.get_remote().close()
            self._remote = None
        except OSError:
            _LOGGER.debug("Could not establish connection.")

    def volume_up(self):
        """Volume up the media player."""
        self.send_key('KEY_VOLUP')

    def volume_down(self):
        """Volume down media player."""
        self.send_key('KEY_VOLDOWN')

    def mute_volume(self, mute):
        """Send mute command."""
        self.send_key('KEY_MUTE')

    def media_play_pause(self):
        """Simulate play pause media player."""
        if self._playing:
            self.media_pause()
        else:
            self.media_play()

    def media_play(self):
        """Send play command."""
        self._playing = True
        self.send_key('KEY_PLAY')

    def media_pause(self):
        """Send media pause command to media player."""
        self._playing = False
        self.send_key('KEY_PAUSE')

    def media_next_track(self):
        """Send next track command."""
        self.send_key('KEY_FF')

    def media_previous_track(self):
        """Send the previous track command."""
        self.send_key('KEY_REWIND')

    async def async_play_media(self, media_type, media_id, **kwargs):
        """Support changing a channel."""
        if media_type != MEDIA_TYPE_CHANNEL:
            _LOGGER.error('Unsupported media type')
            return

        # media_id should only be a channel number
        try:
            cv.positive_int(media_id)
        except vol.Invalid:
            _LOGGER.error('Media ID must be positive integer')
            return

        for digit in media_id:
            await self.hass.async_add_job(self.send_key, 'KEY_' + digit)
            await asyncio.sleep(KEY_PRESS_TIMEOUT, self.hass.loop)

    def turn_on(self):
        """Turn the media player on."""
        if self._mac:
            self._wol.send_magic_packet(self._mac)
        else:
            self.send_key('KEY_POWERON')

    def is_tv_on(self):
        if self._config["port"] == 8002:
            base_url = "https://{}:{}/api/v2/"
        else:
            base_url = "http://{}:{}/api/v2/"
        url = base_url.format(self._config['host'], self._config['port'])
        try:
            res = requests.get(url, timeout=1, verify=False)
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.HTTPError,
                requests.exceptions.ReadTimeout):
            self._state = STATE_OFF
            self._remote = None
            return False
        if res is not None and res.status_code == 200:
            self._state = STATE_ON
            return True
        else:
            _LOGGER.info("Error status returned when checking if TV is on: %s", res.status_code)
            self._state = STATE_OFF
            self._remote = None
            return False

    def select_source(self, source):
        """Set source of TV using remote keys"""
        max_right_clicks = 1
        for key in self._source_list:
            if self._source_list[key] > max_right_clicks:
                max_right_clicks = self._source_list[key]

        right_clicks = self._source_list[source]
        if self._state == STATE_ON:
            self.send_key('KEY_EXIT')
        if self._state == STATE_ON:
            self.send_key('KEY_EXIT')
            self.send_key('KEY_SOURCE')
            for x in range(max_right_clicks + 3):
                self.send_key('KEY_LEFT')
            for x in range(right_clicks):
                self.send_key('KEY_RIGHT')
            self.send_key('KEY_ENTER')
        else:
            _LOGGER.info("TV is powered off, not selecting source: %s", source)


    def set_volume_level(self, volume):
        """Set source of TV"""
        volume_level = round(float(volume) * 100)
        self.set_property('volume', volume_level)

