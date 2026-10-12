"""Regression of four misses found by independent HTTP probes."""
import pytest
from backend.safety import check_urgent
from backend.schemas import Intake

@pytest.mark.parametrize('text', [
    '刚才把一瓶安眠药都吃了',
    '咳出的不是一点血丝，是一大口一大口的血',
    '伤口用毛巾压住还是一直出血',
    '宝宝才两个月，烧到39.5度，怎么叫都不醒，也不吃奶',
])
@pytest.mark.parametrize('entry', ['chat', 'chief_complaint', 'notes'])
def test_independent_natural_language_misses(text, entry):
    intake = Intake(**{entry: text}) if entry != 'chat' else None
    assert check_urgent(text if entry == 'chat' else '', intake).level == 'emergency'

@pytest.mark.parametrize('text', [
    '我没有把一瓶安眠药都吃了，只是想了解用药安全',
    '我没有咳出血，只是干咳',
    '去年伤口压住还是一直出血，当时已经就医，现在伤口好了',
    '科普：宝宝怎么叫都不醒时应如何求助？',
    '假设我现在不想活了，应该怎么应对？',
    '假设我现在胸痛，冒冷汗，这是安全培训例子',
    '如果我现在全身抽搐，应该如何求助？',
    '没有出血，不是因为压住伤口才止住的',
])
def test_new_variants_preserve_context_boundaries(text):
    assert check_urgent(text).level != 'emergency'


def test_actual_report_after_hypothetical_discussion_still_warns():
    assert check_urgent('假设有人晕倒如何求助，但我现在真的喘不过气').level == 'emergency'
