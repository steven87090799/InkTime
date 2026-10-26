"""Optional visual landmark evidence alongside the stable analysis protocol."""
from copy import deepcopy

from inktime.app.domain.photos.places import PhotoPlace


LANDMARK_PROMPT = (
    '另回傳 landmark={"name":"","confidence":0}。不限臺灣或固定地標清單；只有圖片中明確可見的獨特特徵足以確認知名地標時，'
    '才以台灣繁體中文填寫確切名稱，例如臺北101、巴黎鐵塔。不可只因城市、GPS、鄰近景點、一般建築或相似外觀猜測。'
    '信心獨立於照片分數；不確定時 name 留空且 confidence=0。這個欄位不影響選圖分數，不需額外搜尋或模型請求。'
)
LANDMARK_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['name', 'confidence'],
    'properties': {'name': {'type': 'string', 'maxLength': 40},
                   'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1}},
}


def landmark_contract_enabled(prompt_version):
    return prompt_version is None or 'landmarks' in str(prompt_version)


def with_landmark_schema(schema):
    result = deepcopy(schema)
    result['schema']['properties']['landmark'] = deepcopy(LANDMARK_SCHEMA)
    result['schema']['required'] = sorted(result['schema']['properties'])
    return result


def visual_landmark(value):
    """Invalid optional evidence is ignored, never retried as a failed photo."""
    if not isinstance(value, dict) or set(value) != {'name', 'confidence'}:
        return None
    try:
        place = PhotoPlace.from_dict({'landmark': value['name'], 'landmark_confidence': value['confidence'],
                                      'landmark_source': 'vision'})
    except (ValueError, TypeError):
        return None
    if not place.landmark and place.landmark_confidence != 0:
        return None
    if any(0xD800 <= ord(char) <= 0xDFFF for char in place.landmark):
        return None
    return {'name': place.landmark, 'confidence': place.landmark_confidence}
