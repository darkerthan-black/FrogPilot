from collections import defaultdict
from dataclasses import dataclass
from enum import Enum, IntFlag, StrEnum
from typing import Dict, List, Union

from cereal import car
from openpilot.common.numpy_fast import interp
from openpilot.common.params import Params
from openpilot.selfdrive.car import dbc_dict
from openpilot.selfdrive.car.docs_definitions import CarFootnote, CarHarness, CarInfo, CarParts, Column
Ecu = car.CarParams.Ecu


class CarControllerParams:
  STEER_MAX = 300  # GM limit is 3Nm. Used by carcontroller to generate LKA output
  STEER_STEP = 3  # Active control frames per command (~33hz)
  INACTIVE_STEER_STEP = 10  # Inactive control frames per command (10hz)
  STEER_DELTA_UP = 10  # Delta rates require review due to observed EPS weakness
  STEER_DELTA_DOWN = 15
  STEER_DRIVER_ALLOWANCE = 65
  STEER_DRIVER_MULTIPLIER = 4
  STEER_DRIVER_FACTOR = 100
  NEAR_STOP_BRAKE_PHASE = 0.25  # m/s
  SNG_INTERCEPTOR_GAS = 18. / 255.
  SNG_TIME = 30  # frames until the above is reached

  # Heartbeat for dash "Service Adaptive Cruise" and "Service Front Camera"
  ADAS_KEEPALIVE_STEP = 100
  CAMERA_KEEPALIVE_STEP = 100

  # Allow small margin below -3.5 m/s^2 from ISO 15622:2018 since we
  # perform the closed loop control, and might need some
  # to apply some more braking if we're on a downhill slope.
  # Our controller should still keep the 2 second average above
  # -3.5 m/s^2 as per planner limits
  ACCEL_MAX = 2.  # m/s^2
  ACCEL_MAX_PLUS = 4.  # m/s^2
  ACCEL_MIN = -4.  # m/s^2

  def __init__(self, CP):
    # Gas/brake lookups
    self.ZERO_GAS = 6144  # Coasting
    self.MAX_BRAKE = 400  # ~ -4.0 m/s^2 with regen

    if CP.carFingerprint in CAMERA_ACC_CAR and CP.carFingerprint not in CC_ONLY_CAR:
      self.MAX_GAS = 7496
      self.MAX_ACC_REGEN = 5610
      self.INACTIVE_REGEN = 5650
      # Camera ACC vehicles have no regen while enabled.
      # Camera transitions to MAX_ACC_REGEN from ZERO_GAS and uses friction brakes instantly
      max_regen_acceleration = 0.

      if CP.carFingerprint in SLOW_ACC:
        self.MAX_GAS = 8650

    elif CP.carFingerprint in SDGM_CAR:
      self.MAX_GAS = 3400
      self.MAX_ACC_REGEN = 1514
      self.INACTIVE_REGEN = 1554
      max_regen_acceleration = 0.

    else:
      self.MAX_GAS = 7168  # Safety limit, not ACC max. Stock ACC >8192 from standstill.
      self.MAX_ACC_REGEN = 5500  # Max ACC regen is slightly less than max paddle regen
      self.INACTIVE_REGEN = 5500
      # ICE has much less engine braking force compared to regen in EVs,
      # lower threshold removes some braking deadzone
      max_regen_acceleration = -1. if CP.carFingerprint in EV_CAR else -0.1

    self.GAS_LOOKUP_BP = [max_regen_acceleration, 0., self.ACCEL_MAX]
    self.GAS_LOOKUP_V = [self.MAX_ACC_REGEN, self.ZERO_GAS, self.MAX_GAS]

    self.BRAKE_LOOKUP_BP = [self.ACCEL_MIN, max_regen_acceleration]
    self.BRAKE_LOOKUP_V = [self.MAX_BRAKE, 0.]

  # determined by letting Volt regen to a stop in L gear from 89mph,
  # and by letting off gas and allowing car to creep, for determining
  # the positive threshold values at very low speed
  EV_GAS_BRAKE_THRESHOLD_BP = [1.29, 1.52, 1.55, 1.6, 1.7, 1.8, 2.0, 2.2, 2.5, 5.52, 9.6, 20.5, 23.5, 35.0] # [m/s]
  EV_GAS_BRAKE_THRESHOLD_V = [0.0, -0.14, -0.16, -0.18, -0.215, -0.255, -0.32, -0.41, -0.5, -0.72, -0.895, -1.125, -1.145, -1.16] # [m/s^s]

  def update_ev_gas_brake_threshold(self, v_ego):
    gas_brake_threshold = interp(v_ego, self.EV_GAS_BRAKE_THRESHOLD_BP, self.EV_GAS_BRAKE_THRESHOLD_V)
    self.EV_GAS_LOOKUP_BP = [gas_brake_threshold, max(0., gas_brake_threshold), self.ACCEL_MAX]
    self.EV_BRAKE_LOOKUP_BP = [self.ACCEL_MIN, gas_brake_threshold]


class CAR(StrEnum):
  HOLDEN_ASTRA = "HOLDEN ASTRA RS-V BK 2017"
  VOLT = "CHEVROLET VOLT PREMIER 2017"
  CADILLAC_ATS = "CADILLAC ATS Premium Performance 2018"
  MALIBU = "CHEVROLET MALIBU PREMIER 2017"
  ACADIA = "GMC ACADIA DENALI 2018"
  BUICK_LACROSSE = "BUICK LACROSSE 2017"
  BUICK_REGAL = "BUICK REGAL ESSENCE 2018"
  ESCALADE = "CADILLAC ESCALADE 2017"
  ESCALADE_ESV = "CADILLAC ESCALADE ESV 2016"
  ESCALADE_ESV_2019 = "CADILLAC ESCALADE ESV 2019"
  BOLT_EUV = "CHEVROLET BOLT EUV 2022"
  SILVERADO = "CHEVROLET SILVERADO 1500 2020"
  EQUINOX = "CHEVROLET EQUINOX 2019"
  TRAILBLAZER = "CHEVROLET TRAILBLAZER 2021"
  # Separate car def is required when there is no ASCM
  # (for now) unless there is a way to detect it when it has been unplugged...
  VOLT_CC = "CHEVROLET VOLT NO ACC"
  BOLT_CC = "CHEVROLET BOLT EV NO ACC"
  EQUINOX_CC = "CHEVROLET EQUINOX NO ACC"
  SUBURBAN = "CHEVROLET SUBURBAN PREMIER 2016"
  SUBURBAN_CC = "CHEVROLET SUBURBAN NO ACC"
  YUKON_CC = "GMC YUKON NO ACC"
  CT6_CC = "CADILLAC CT6 NO ACC"
  TRAILBLAZER_CC = "CHEVROLET TRAILBLAZER 2024 NO ACC"
  XT4 = "CADILLAC XT4 2023"
  TRAX = "CHEVROLET TRAX 2024"


class Footnote(Enum):
  OBD_II = CarFootnote(
    'Requires a <a href="https://github.com/commaai/openpilot/wiki/GM#hardware" target="_blank">community built ASCM harness</a>. ' +
    '<b><i>NOTE: disconnecting the ASCM disables Automatic Emergency Braking (AEB).</i></b>',
    Column.MODEL)


@dataclass
class GMCarInfo(CarInfo):
  package: str = "Adaptive Cruise Control (ACC)"

  def init_make(self, CP: car.CarParams):
    if CP.networkLocation == car.CarParams.NetworkLocation.fwdCamera:
      self.car_parts = CarParts.common([CarHarness.gm])
    else:
      self.car_parts = CarParts.common([CarHarness.obd_ii])
      self.footnotes.append(Footnote.OBD_II)


CAR_INFO: Dict[str, Union[GMCarInfo, List[GMCarInfo]]] = {
  CAR.HOLDEN_ASTRA: GMCarInfo("Holden Astra 2017"),
  CAR.VOLT: GMCarInfo("Chevrolet Volt 2017-18", min_enable_speed=0, video_link="https://youtu.be/QeMCN_4TFfQ"),
  CAR.CADILLAC_ATS: GMCarInfo("Cadillac ATS Premium Performance 2018"),
  CAR.MALIBU: GMCarInfo("Chevrolet Malibu Premier 2017"),
  CAR.ACADIA: GMCarInfo("GMC Acadia 2018", video_link="https://www.youtube.com/watch?v=0ZN6DdsBUZo"),
  CAR.BUICK_LACROSSE: GMCarInfo("Buick LaCrosse 2017-19", "Driver Confidence Package 2"),
  CAR.BUICK_REGAL: GMCarInfo("Buick Regal Essence 2018"),
  CAR.ESCALADE: GMCarInfo("Cadillac Escalade 2017", "Driver Assist Package"),
  CAR.ESCALADE_ESV: GMCarInfo("Cadillac Escalade ESV 2016", "Adaptive Cruise Control (ACC) & LKAS"),
  CAR.ESCALADE_ESV_2019: GMCarInfo("Cadillac Escalade ESV 2019", "Adaptive Cruise Control (ACC) & LKAS"),
  CAR.BOLT_EUV: [
    GMCarInfo("Chevrolet Bolt EUV 2022-23", "Premier or Premier Redline Trim without Super Cruise Package", video_link="https://youtu.be/xvwzGMUA210"),
    GMCarInfo("Chevrolet Bolt EV 2022-23", "2LT Trim with Adaptive Cruise Control Package"),
  ],
  CAR.SILVERADO: [
    GMCarInfo("Chevrolet Silverado 1500 2020-21", "Safety Package II"),
    GMCarInfo("GMC Sierra 1500 2020-21", "Driver Alert Package II", video_link="https://youtu.be/5HbNoBLzRwE"),
  ],
  CAR.EQUINOX: GMCarInfo("Chevrolet Equinox 2019-22"),
  CAR.TRAILBLAZER: GMCarInfo("Chevrolet Trailblazer 2021-22"),

  CAR.VOLT_CC: GMCarInfo("Chevrolet Volt No ACC"),
  CAR.BOLT_CC: GMCarInfo("Chevrolet Bolt No ACC"),
  CAR.EQUINOX_CC: GMCarInfo("Chevrolet Equinox No ACC"),
  CAR.SUBURBAN: GMCarInfo("Chevrolet Suburban Premier 2016-2020"),
  CAR.SUBURBAN_CC: GMCarInfo("Chevrolet Suburban No ACC"),
  CAR.YUKON_CC: GMCarInfo("GMC Yukon No ACC"),
  CAR.CT6_CC: GMCarInfo("Cadillac CT6 No ACC"),
  CAR.TRAILBLAZER_CC: GMCarInfo("Chevrolet Trailblazer 2024 No ACC"),
  CAR.XT4: GMCarInfo("Cadillac XT4 2023", "Driver Assist Package"),
  CAR.TRAX: GMCarInfo("Chevrolet TRAX 2024"),
}


class CruiseButtons:
  INIT = 0
  UNPRESS = 1
  RES_ACCEL = 2
  DECEL_SET = 3
  MAIN = 5
  CANCEL = 6

class AccState:
  OFF = 0
  ACTIVE = 1
  FAULTED = 3
  STANDSTILL = 4

class CanBus:
  POWERTRAIN = 0
  OBSTACLE = 1
  CAMERA = 2
  CHASSIS = 2
  LOOPBACK = 128
  DROPPED = 192

class GMFlags(IntFlag):
  PEDAL_LONG = 1
  CC_LONG = 2
  NO_CAMERA = 4
  NO_ACCELERATOR_POS_MSG = 8


GM_RX_OFFSET = 0x400

DBC: Dict[str, Dict[str, str]] = defaultdict(lambda: dbc_dict('gm_global_a_powertrain_generated', 'gm_global_a_object', chassis_dbc='gm_global_a_chassis'))
DBC[CAR.VOLT] = dbc_dict('gm_global_a_powertrain_volt', 'gm_global_a_object', chassis_dbc='gm_global_a_chassis')
DBC[CAR.VOLT_CC] = DBC[CAR.VOLT]

EV_CAR = {CAR.VOLT, CAR.BOLT_EUV, CAR.VOLT_CC, CAR.BOLT_CC}
CC_ONLY_CAR = {CAR.VOLT_CC, CAR.BOLT_CC, CAR.EQUINOX_CC, CAR.SUBURBAN_CC, CAR.YUKON_CC, CAR.CT6_CC, CAR.TRAILBLAZER_CC}

# We're integrated at the Safety Data Gateway Module on these cars
SDGM_CAR = {CAR.XT4}

# Slow acceleration cars
SLOW_ACC = {CAR.SILVERADO} if Params().get_bool("GasRegenCmd") else set()

# We're integrated at the camera with VOACC on these cars (instead of ASCM w/ OBD-II harness)
CAMERA_ACC_CAR = {CAR.BOLT_EUV, CAR.SILVERADO, CAR.EQUINOX, CAR.TRAILBLAZER, CAR.TRAX}
CAMERA_ACC_CAR.update({CAR.VOLT_CC, CAR.BOLT_CC, CAR.EQUINOX_CC, CAR.YUKON_CC, CAR.CT6_CC, CAR.TRAILBLAZER_CC})

STEER_THRESHOLD = 1.0
