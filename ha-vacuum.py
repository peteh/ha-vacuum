import logging
import paho.mqtt.client as mqtt
import json
import time
import datetime
import lxml.html.soupparser as sp

import click
import google.auth.transport.grpc
import google.auth.transport.requests
import google.oauth2.credentials

from google.assistant.embedded.v1alpha2 import (
    embedded_assistant_pb2,
    embedded_assistant_pb2_grpc
)

#config variables
VACUUM_NAME = "Robo"
ROOMS =["Livingroom", "Office", "Bathroom", "Toilet", "Kitchen", "Bedroom"]

VACUUM_UNIQUE_ID = "ha-vacuum" # used for mqtt path and unique id for homeassistant
MQTT_BROKER = "homeassistant.local"
MQTT_PORT = 1883

# TODO: handle restart of homeassistant
# TODO: make main topic and unique id also configurable
# TODO: unhardcode mqtt topics

VACUUM_ROOMS_UNIQUE_ID = VACUUM_UNIQUE_ID + "-rooms"
# internal config vars
DELAY_AFTER_STATE_UPDATE = 3 * 60 # delay between state updates from google home
ASSISTANT_API_ENDPOINT = 'embeddedassistant.googleapis.com'
DEFAULT_GRPC_DEADLINE = 60 * 3 + 5
PLAYING = embedded_assistant_pb2.ScreenOutConfig.PLAYING
HOMEASSISTANT_STATUS_TOPIC = "homeassistant/status"


def log_assist_request_without_audio(assist_request):
    """Log AssistRequest fields without audio data."""
    if logging.getLogger().isEnabledFor(logging.DEBUG):
        resp_copy = embedded_assistant_pb2.AssistRequest()
        resp_copy.CopyFrom(assist_request)
        if len(resp_copy.audio_in) > 0:
            size = len(resp_copy.audio_in)
            resp_copy.ClearField('audio_in')
            logging.debug('AssistRequest: audio_in (%d bytes)',
                          size)
            return
        logging.debug('AssistRequest: %s', resp_copy)


def log_assist_response_without_audio(assist_response):
    """Log AssistResponse fields without audio data."""
    if logging.getLogger().isEnabledFor(logging.DEBUG):
        resp_copy = embedded_assistant_pb2.AssistResponse()
        resp_copy.CopyFrom(assist_response)
        has_audio_data = (resp_copy.HasField('audio_out') and
                          len(resp_copy.audio_out.audio_data) > 0)
        if has_audio_data:
            size = len(resp_copy.audio_out.audio_data)
            resp_copy.audio_out.ClearField('audio_data')
            if resp_copy.audio_out.ListFields():
                logging.debug('AssistResponse: %s audio_data (%d bytes)',
                              resp_copy,
                              size)
            else:
                logging.debug('AssistResponse: audio_data (%d bytes)',
                              size)
            return
        logging.debug('AssistResponse: %s', resp_copy)


class TextBasedAssistant(object):
    """Sample Assistant that supports text based conversations.

    Args:
      language_code: language for the conversation.
      device_model_id: identifier of the device model.
      device_id: identifier of the registered device instance.
      channel: authorized gRPC channel for connection to the
        Google Assistant API.
      deadline_sec: gRPC deadline in seconds for Google Assistant API call.
    """

    def __init__(self, language_code, device_model_id, device_id,
                 channel, deadline_sec):
        self.language_code = language_code
        self.device_model_id = device_model_id
        self.device_id = device_id
        self.conversation_state = None
        # Force reset of first conversation.
        self.is_new_conversation = True
        self.assistant = embedded_assistant_pb2_grpc.EmbeddedAssistantStub(
            channel
        )
        self.deadline = deadline_sec

    def __enter__(self):
        return self

    def __exit__(self, etype, e, traceback):
        if e:
            return False

    def _textFromHtml(self, html_response):
        if(html_response is None):
            return None
        tree = sp.fromstring(html_response)
        matches = tree.xpath(".//div[@class='show_text_content']")
        if(len(matches) == 0):
            return None
        return matches[0].text

    def assist(self, text_query):
        """Send a text request to the Assistant and playback the response.
        """
        def iter_assist_requests():
            config = embedded_assistant_pb2.AssistConfig(
                audio_out_config=embedded_assistant_pb2.AudioOutConfig(
                    encoding='LINEAR16',
                    sample_rate_hertz=16000,
                    volume_percentage=0,
                ),
                dialog_state_in=embedded_assistant_pb2.DialogStateIn(
                    language_code=self.language_code,
                    conversation_state=self.conversation_state,
                    is_new_conversation=self.is_new_conversation,
                ),
                device_config=embedded_assistant_pb2.DeviceConfig(
                    device_id=self.device_id,
                    device_model_id=self.device_model_id,
                ),
                text_query=text_query,
            )
            # Force new conversation and don't allow questions
            self.is_new_conversation = True
            config.screen_out_config.screen_mode = PLAYING
            req = embedded_assistant_pb2.AssistRequest(config=config)
            log_assist_request_without_audio(req)
            yield req

        text_response = None
        html_response = None
        for resp in self.assistant.Assist(iter_assist_requests(),
                                          self.deadline):
            log_assist_response_without_audio(resp)
            if resp.screen_out.data:
                html_response = resp.screen_out.data
                if not resp.dialog_state_out.supplemental_display_text:
                    text_response = self._textFromHtml(html_response)
            if resp.dialog_state_out.conversation_state:
                conversation_state = resp.dialog_state_out.conversation_state
                self.conversation_state = conversation_state

            if resp.dialog_state_out.supplemental_display_text and text_response:
                text_response = resp.dialog_state_out.supplemental_display_text
        return text_response, html_response


class VacuumCommander():
    def __init__(self, assistant):
        self._assistant = assistant
        self._state = "idle"
        self._lastUpdate = datetime.datetime.min

    def clean(self):
        response_text, response_html = self._assistant.assist("Start cleaning")
        if "starting" in response_text:
            self._setState("cleaning")
            return True
        return False

    def cleanRoom(self, roomName):
        response_text, response_html = self._assistant.assist(
            "Clean %s" % (roomName))
        if "starting" in response_text:
            self._setState("cleaning")
            return True
        return False

    def pause(self):
        response_text, response_html = self._assistant.assist("Pause cleaning")
        if "pausing" in response_text:
            if self.getState() == "cleaning": # only set to pause if we were cleaning
                self._setState("pause")
            return True
        return False

    def stop(self):
        response_text, response_html = self._assistant.assist("Stop cleaning")
        if "stopping" in response_text:
            self._setState("idle")
            return True
        return False

    def return_to_base(self):
        response_text, response_html = self._assistant.assist(
            "Send vacuum to dock")
        if "docking" in response_text:
            self._setState("returning")
            return True
        return False

    def locate(self):
        response_text, response_html = self._assistant.assist("Locate vacuum")
        return "locating" in response_text

    def getState(self):
        return self._state

    def updateState(self):
        lastUpdateDiffS = (datetime.datetime.now() - self._lastUpdate).total_seconds() 
        if lastUpdateDiffS < DELAY_AFTER_STATE_UPDATE: 
            logging.debug("Skipping state update (last: %d seconds ago)", lastUpdateDiffS)
            return
        
        response_text, response_html = self._assistant.assist(
            "Is vacuum docked?")
        if "is docked" in response_text:
            logging.debug("GHome is docked?: %s", response_text)
            self._setState("docked")
        else:
            response_text, response_html = self._assistant.assist(
            "What is vacuum doing?")
            logging.debug("GHome what's vacuum doing?: %s", response_text)
            if "is running" in response_text:
                self._setState("cleaning")
            elif "is paused" in response_text:
                self._setState("paused")
            elif "isn't running" in response_text: # not docked and not running/paused = idle
                self._setState("idle")
            else: 
                raise Exception("Unknown state: %s" % (response_text))
        self._lastUpdate = datetime.datetime.now()
    
    def _setState(self, newState):
        if self._state != newState:
            logging.info("New State %s", newState)
        self._state = newState
        self._lastUpdate = datetime.datetime.now()

    def __str__(self):
        return self._state

class MqttHAClient():
    def __init__(self, vacuumCommander):
        # TODO: move to extra method
        self._client = mqtt.Client()
        self._client.on_connect = self._onConnect
        self._client.on_message = self._onMessage
        self._client.connect("homeassistant.local")
        self._client.loop_start()
        self._vacuumCommander = vacuumCommander
    
    def _onConnect(self, client, userdata, flags, rc):
        self._client.subscribe("%s/cmd" % VACUUM_UNIQUE_ID)
        self._client.subscribe("%s/roomselect/cmd" % VACUUM_UNIQUE_ID)
        self._client.subscribe(HOMEASSISTANT_STATUS_TOPIC)
        self._publishConfig()
    
    def _onMessage(self, client, userdata, msg):
        if msg.topic == "%s/cmd" % (VACUUM_UNIQUE_ID):
            command = msg.payload.decode("utf-8")
            logging.info("Mqtt Command: %s", command)
            if command == "start":
                self._vacuumCommander.clean()
            elif command == "stop":
                self._vacuumCommander.stop()
            elif command == "return_to_base":
                self._vacuumCommander.return_to_base()
            elif command == "pause":
                self._vacuumCommander.pause()
            elif command == "locate":
                self._vacuumCommander.locate()
        elif msg.topic == "%s/roomselect/cmd" % VACUUM_UNIQUE_ID:
            room = msg.payload.decode("utf-8")
            if room.lower() != "(none)":
                self._vacuumCommander.cleanRoom(room)

        elif msg.topic == HOMEASSISTANT_STATUS_TOPIC:
            command = msg.payload.decode("utf-8")
            if command.lower() == "online":
                self._publishConfig()
                
    def _publishConfigVacuum(self):
        config = {}
        topic = "homeassistant/vacuum/%s/config" % (VACUUM_UNIQUE_ID)
        config["~"] = VACUUM_UNIQUE_ID
        config["availability_topic"] = "~"
        config["name"] = VACUUM_NAME
        config["unique_id"] = VACUUM_UNIQUE_ID
        config["command_topic"] = "~/cmd"
        config["schema"] = "state"
        config["state_topic"] = "~/state"
        config["supported_features"] = ["start", "stop", "return_home", "pause", "status", "locate"]
        logging.info("Publishing homeassistant config for vacuum on %s", topic)
        self._client.publish(topic, json.dumps(config))
        self._client.publish(VACUUM_UNIQUE_ID, "online")

    def _publishConfigRoomSelect(self):
        roomselectTopic = VACUUM_UNIQUE_ID + "/roomselect"
        config = {}
        topic = "homeassistant/select/%s/config" % (VACUUM_ROOMS_UNIQUE_ID)
        config["~"] = roomselectTopic
        config["availability_topic"] = "~"
        config["name"] = VACUUM_NAME + " Rooms"
        config["unique_id"] = VACUUM_ROOMS_UNIQUE_ID
        config["command_topic"] = "~/cmd";
        config["options"] = ["(none)"] + ROOMS
        logging.info("Publishing homeassistant config for roomselect on %s", topic)
        self._client.publish(topic, json.dumps(config))
        self._client.publish(roomselectTopic, "online")

    def _publishConfig(self):
        self._publishConfigVacuum()
        self._publishConfigRoomSelect()
    
    def publishState(self):
        state = {}
        state["state"] = self._vacuumCommander.getState()
        self._client.publish("ha-vacuum/state", json.dumps(state))


@click.command()
@click.option('--cli', '-c', is_flag=True, default=False,
              help='Enable CLI mode.')
@click.option('--verbose', '-v', is_flag=True, default=False,
              help='Verbose logging.')
def main(cli, verbose, *args, **kwargs):
    credentials = "credentials.json"
    lang = "en-US"
    device_model_id = "googleassistantbridge" #TODO: make this config param 
    device_id = device_model_id + "-1"
    grpc_deadline = DEFAULT_GRPC_DEADLINE

    # Setup logging.
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO)

    # Load OAuth 2.0 credentials.
    try:
        with open(credentials, 'r') as f:
            credentials = google.oauth2.credentials.Credentials(token=None,
                                                                **json.load(f))
            http_request = google.auth.transport.requests.Request()
            credentials.refresh(http_request)
    except Exception as e:
        logging.error('Error loading credentials: %s', e)
        logging.error('Run google-oauthlib-tool to initialize '
                      'new OAuth 2.0 credentials.')
        return

    # Create an authorized gRPC channel.
    grpc_channel = google.auth.transport.grpc.secure_authorized_channel(
        credentials, http_request, ASSISTANT_API_ENDPOINT)
    logging.info('Connecting to %s', ASSISTANT_API_ENDPOINT)

    assistant = TextBasedAssistant(lang, device_model_id, device_id, 
                                    grpc_channel, grpc_deadline)
    if cli:
        while True:
            query = click.prompt('')
            click.echo('<you> %s' % query)
            response_text, response_html = assistant.assist(query)
            #if response_html:
            #    with open('response.html', 'w') as f:
            #        f.write(response_html.decode('utf-8'))
            if response_text:
                click.echo('<@assistant> %s' % response_text)
    else:
        vacuum_commander = VacuumCommander(assistant)
        mqtt_client = MqttHAClient(vacuum_commander)

        while True:
            vacuum_commander.updateState()
            mqtt_client.publishState()
            time.sleep(10)


if __name__ == '__main__':
    main()
