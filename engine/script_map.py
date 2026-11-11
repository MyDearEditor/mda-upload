"""Where in the sales script is the agent right now?

Every MyDear Academy agent reads one of two scripts (Mason's Sales Process or the LFL Closer Handbook) or a
blend of them, so a call walks through the same sections in the same order every time: intro, how we work,
underwriting, credibility, discovery, recap, product, carrier, three options, the close, confirm
affordability, explain the process, the app, wrap-up. The agent is never word for word - but a few matched
key phrases per section are enough to place them, and sections only ever move forward, so a stray match
can't drag the call backwards.

Knowing the section does four jobs: the on-screen "live call" cards (Discovery / The Close / Confirm
Affordability!), context for the clip picker and the hook writer, a preference for the sections that make
good reels, and a hard no-clip zone for the application (social, routing and account numbers).
"""
import re
from difflib import SequenceMatcher

# key, label (the on-screen card, in Mason's wording), phrases from both scripts, flags
#   card  - put a card up when the agent enters this section
#   zone  - 'app' is never cut into a clip; 'skip' is not worth a clip (health questions)
SECTIONS = [
    {'key': 'intro', 'label': 'The Intro', 'card': True, 'phrases': [
        'getting back to you in regards to the request you sent in',
        'just getting back to you in regards to the whole life',
        'it looks like you listed would be your beneficiary is that right',
        'it shows here that you listed your age as',
        'looking just for yourself or for yourself and a loved one',
        'do not have coverage in place is that accurate',
        'looking to leave something behind for the family or just trying to cover the funeral expenses',
        'looking to add on to what you currently have or just trying to find something more affordable',
        'is there like a two year waiting period for that',
        'how much are they making you pay for that']},
    {'key': 'howwework', 'label': 'How We Work', 'card': True, 'phrases': [
        'just so you know how we work here we are a little different',
        'here is how we do things',
        'find the most affordable coverage in the state',
        'we work with all 26 of the life insurance companies',
        'we shop around all 26 companies',
        'my job here is to help you see what is most affordable and to see if we can get you qualified',
        'my job is to help you find what you might qualify for and make sure it actually fits your budget']},
    {'key': 'underwriting', 'label': 'Underwriting', 'card': False, 'zone': 'skip', 'phrases': [
        'so to see how we could best do that are you by chance a smoker',
        'are you a smoker or using any type of nicotine or tobacco products',
        'what is an accurate height and weight',
        'what is your approximate height and weight',
        'any history of heart issues strokes or cancer',
        'any lung issues copd asthma inhalers',
        'by chance any diabetes',
        'any diabetes or elevated sugar in the past',
        'any anxiety or depression',
        'any kidney or liver issues',
        'taking any blood pressure heart medications',
        'active checking or savings account i just need to hit yes or no',
        'in the last 12 months have you had 3 or more overnight hospital stays',
        'any hospitalizations in the last 2 years']},
    {'key': 'credibility', 'label': 'Credibility', 'card': True, 'phrases': [
        'that is going to be everything i need here before we jump into it',
        'really important to me with the way the world is today that you know exactly who you are speaking to',
        'do you happen to have a pen and paper nearby',
        'do you have a paper or a pen',
        'we are speaking on my work line right now but i like to give out my direct number',
        'write down my direct phone number',
        'does require i share my national producer number with you',
        'i just have to share with you my npn that is my national producer number',
        'this just lets you know i am licensed in the state gone through my background check',
        'you will find my eye color and hair color']},
    {'key': 'discovery', 'label': 'Discovery', 'card': True, 'phrases': [
        'that is enough about me here kind of fill me in',
        'kinda fill me in what had you motivated to start looking around for some life insurance now',
        'what had you motivated to start looking around',
        'what is the goal of the life insurance what do you want to make sure is taken care of',
        'what do you specifically want to make sure is taken care of',
        'god forbid if something was to happen who would be like that one person responsible for planning all the funeral arrangements',
        'who would be responsible for paying for your burial expenses',
        'would they be in a position where they could come out of pocket 10 to 15 thousand dollars',
        'be in a position where they could come out of pocket for your burial expenses',
        'what would they have to do start a gofundme or borrow money',
        'what would they have to do',
        'has someone in your life passed away with or without life insurance',
        'have you been looking around for something like this for a while now or is it something you just realized you kinda need',
        'have you just not been able to find anything comfortable and affordable for you or have you not been able to qualify',
        'would you say having just a little something in place would that be better than having nothing at all']},
    {'key': 'recap', 'label': 'Recap', 'card': True, 'phrases': [
        'okay so it just sounds like as a you just want to make sure that when that time comes',
        'so it sounds like as a you just want to make sure',
        'understood so it sounds like we are not really trying to leave any money',
        'is it just to make sure the funeral expenses are paid for',
        'sounds like you just want to make sure that when that time comes',
        'is in good hands that does not need to be worried about',
        'can just focus on grieving is that basically what i am hearing',
        'if you do not get this coverage in place your beneficiary will have to is that about right',
        'is never left with the burden of your burial expenses',
        'so it sounds like you really value having life insurance you just need to make sure we can find a comfortable option',
        'just so we are on the same exact page here this is pretty straight forward',
        'have i left anything out there']},
    {'key': 'coverage', 'label': 'Coverage Amount', 'card': True, 'phrases': [
        'when it comes to the coverage amount were you planning more of a traditional earth burial or more of just like a cremation',
        'are you planning for a formal burial or are you going to go the cremation route',
        'generally they recommend 15 to 20 thousand for an earth burial',
        'is that kind of in the ball park of what you were thinking',
        'we will take a look at a few different options just to see what works best for you']},
    {'key': 'zombies', 'label': 'Pre-Handle The Objection', 'card': True, 'phrases': [
        'i do work with people here on fixed incomes and social security disability',
        'i work with people who are on fixed incomes',
        'if we could possibly find something that is comfortable and affordable for you every month would there be anything holding you back',
        'would there be anything holding you back from seeing if we could even get you qualified today',
        'anything holding you back from seeing if you could get approved today']},
    {'key': 'product', 'label': 'Explain The Product', 'card': True, 'phrases': [
        'what we are going to be looking at here is whole life insurance and you let me know if this is what you are looking for',
        'the monthly payment will never go up the coverage amount will never go down and it is something that will never expire',
        'it will be there for your whole life and it is guaranteed to pay out',
        'so you can go to bed knowing that no matter what is taken care of',
        'is that kind of what you are looking for']},
    {'key': 'carrier', 'label': 'Pitch The Carrier', 'card': True, 'phrases': [
        'i have everything plugged in here and you should be eligible for the best option statewide out of all 26 companies',
        'should be eligible for the best option statewide',
        'it is with a company called have you ever heard of them before',
        'they have these commercials and what not on tv',
        'they have been in business since',
        'they have not missed a payout claim',
        'backed by the better business bureau with an a plus rating',
        'one they are going to be the most affordable option in the state but two they are going to be one of just a few companies that will not put you in a 2 year waiting period',
        'one of just a few companies that will not put you in any sort of a waiting period',
        'will automatically put you in a two year waiting period but will not',
        'so this is day one coverage and that just means if god forbid something was to happen the day after this policy goes into effect',
        'would get a check in the mail within 24 to 48 hours',
        'so it is always just really important for me to point that out',
        'one of the only companies with your that will not make you wait 2 years',
        'for the first 2 years god forbid something were to happen to you from natural causes they will pay every penny you paid into the policy',
        'free members benefits',
        'funeral concierge', 'everest program']},
    {'key': 'options', 'label': 'The Three Options', 'card': True, 'phrases': [
        'they have given us 3 options that they recommend but keep in mind this is just what they recommend',
        'they have 3 options that they recommend for you',
        'we can always go up down sideways based on your budget and needs',
        'the first option here this is more of a starting option for those who are on a fixed income',
        'this is our starter program for those who are on a fixed income',
        'making sure there is enough coverage to take care of the basic expenses',
        'if we can even get you qualified for the that is going to be a month',
        'the second option here this is more of a common option what most of my members would typically go with',
        'this is the most common program that most typically go with',
        'now and well into the future adjusting for inflation',
        'the final option here is for this is going to be more than enough',
        'the last option here is for now i do not know if i would recommend this for you',
        'this might be more than you need right now']},
    {'key': 'close', 'label': 'The Close', 'card': True, 'phrases': [
        'our biggest challenge here is really seeing if we can get you qualified',
        'our biggest challenge here is to see if you can actually qualify',
        'but you kind of fill me in here as a do you feel like we should go with the',
        'do you feel like we should go with the',
        'what would give you the most peace of mind',
        'which would give you the most peace of mind']},
    {'key': 'downsell', 'label': 'Down-Sell & Close', 'card': True, 'phrases': [
        'should we start with the for now and then if we want to down the road we can go up',
        'should we start with like the 15000 for now',
        'then if we want to down the road go up is that what you would prefer',
        'we could start with the smaller option for now and go up down the road',
        'what if we just started with something that is comfortable and affordable']},
    {'key': 'affordability', 'label': 'Confirm Affordability!', 'card': True, 'phrases': [
        'just to make sure i am truly helping here and not putting any strain on the budget',
        'so i know i am truly helping you and not putting any strain on your budget or taking food off the table',
        'that is going to be comfortable and affordable for you every month',
        'will that be comfortable for you not just this month but every month moving forward']},
    {'key': 'process', 'label': 'Explain The Process!', 'card': True, 'phrases': [
        'so kinda how the process works here it is pretty quick',
        'so i will explain how the process works here it is really quick',
        'basically i will take the app info over the phone with you we will do a quick hipaa authorization',
        'basically we just take the application info together over the phone',
        'will ask a few health questions will name your beneficiary and from there we will pick the date you want your policy to start',
        'most of my members like to have their policy start immediately for peace of mind',
        'the soonest i can get yours going is 24 to 48 hours from now',
        'what date would you want your policy to start',
        'would you like to put a middle initial on this policy or leave that off']},
    {'key': 'app', 'label': 'The Application', 'card': False, 'zone': 'app', 'phrases': [
        'so just to confirm the spelling of your name',
        'this is where they have me grab your social for the security verification',
        'your social for the prescription check',
        'whenever you are ready please confirm your social',
        'do i have to give it to you do you need the full social',
        'there is going to be a death certificate and on that death certificate is going to be your social security number',
        'what is the best mailing address to send you this policy in the mail',
        'are we going to be funding this with the checking or savings account',
        'what is the name of the bank that you are with',
        'who do you bank with',
        'they have auto generated the routing number',
        'i am ready for the account number whenever you are',
        'and the account number',
        'i am not giving out my bank information',
        'they do have us put down how you plan on paying for the policy']},
    {'key': 'wrapup', 'label': 'Approved!', 'card': True, 'phrases': [
        'i have some good news for you and some bad news as well which do you want first',
        'the bad news is that you are going to be stuck with me for life',
        'the good news is that we did get you fully approved for that policy',
        'fully approved so congratulations',
        'before we go i just like to recap everything we did',
        'you are going to be getting a text message and an email with all the policy information',
        'i literally promise to be your life insurance agent until death do us part',
        'you are going to have agents calling you knocking on your door telling you they have the best policy since sliced bread',
        'tell them to find a rock and kick it that you have already got a life insurance agent',
        'there is a lot of scammers out there they have all got something that sounds too good to be true',
        'if you get a call from someone else grab your phone and call me']},
    {'key': 'referral', 'label': 'Referrals', 'card': True, 'phrases': [
        'before we go how do you feel now',
        'was this helpful for you today',
        'i do need number as well i want to go over the whole process with them',
        'who else came to mind that might benefit from my help',
        'anybody you can think of that would benefit from my help',
        'is there anybody whose funeral expenses you would be responsible for',
        'if you do not mind just giving me 5 stars']},
]
IDX = {s['key']: i for i, s in enumerate(SECTIONS)}
BY_KEY = {s['key']: s for s in SECTIONS}
# the sections a reel is usually made of; the clip picker is told to lean on these
REEL_SECTIONS = ('intro', 'discovery', 'recap', 'zombies', 'carrier', 'options', 'close', 'affordability',
                 'process', 'wrapup', 'referral')

_STOP = {'the', 'a', 'an', 'to', 'of', 'and', 'or', 'is', 'it', 'that', 'this', 'in', 'on', 'for', 'be',
         'you', 'your', 'i', 'we', 'me', 'my', 'so', 'just', 'here', 'okay', 'well', 'like', 'kind', 'kinda',
         'with', 'are', 'was', 'do', 'does', 'did', 'have', 'has', 'at', 'as', 'if', 'but', 'not', 'any',
         'would', 'could', 'can', 'get', 'got', 'going', 'gonna', 'am', 'us', 'they', 'them', 'there'}
_SYN = {"i'm": 'i am', "you're": 'you are', "we're": 'we are', "they're": 'they are', "it's": 'it is',
        "that's": 'that is', "what's": 'what is', "don't": 'do not', "doesn't": 'does not', "won't": 'will not',
        "can't": 'can not', "gonna": 'going to', "wanna": 'want to', "y'all": 'you all', "i'll": 'i will',
        "we'll": 'we will', "i've": 'i have', "there's": 'there is', "here's": 'here is'}
_NUM = {'twenty': '20', 'thirty': '30', 'forty': '40', 'fifty': '50', 'two': '2', 'three': '3', 'five': '5',
        'ten': '10', 'fifteen': '15', 'seventy': '70', 'dollars': 'dollar', 'thousand': 'thousand'}


def toks(text):
    t = str(text).lower()
    for k, v in _SYN.items():
        t = t.replace(k, v)
    t = re.sub(r"[^a-z0-9' ]", ' ', t)
    out = []
    for w in t.split():
        w = w.strip("'")
        w = _NUM.get(w, w)
        if w and w not in _STOP:
            out.append(w)
    return out


_PH = [[(ph, toks(ph)) for ph in s['phrases']] for s in SECTIONS]


def _coverage(ptoks, wset, wseq):
    """How much of a script phrase a window of speech contains: content tokens present, with a bonus when
    they also come in the phrase's order (so 'option two years waiting' and 'two year waiting period'
    both land, but a bag of loosely shared words does not)."""
    if not ptoks:
        return 0.0
    hit = [t for t in ptoks if t in wset]
    cov = len(hit) / len(ptoks)
    if cov < 0.5 or len(hit) < 3:
        return 0.0
    # order bonus: ratio of the longest in-order run
    sm = SequenceMatcher(None, ptoks, [t for t in wseq if t in set(ptoks)], autojunk=False)
    run = max((b.size for b in sm.get_matching_blocks()), default=0)
    return min(1.0, cov * 0.75 + 0.25 * run / len(ptoks))


def detect(transcript, min_score=0.62):
    """Transcript (whisper segments with 'start','end','text','words') -> ordered section spans.

    Returns {'anchors': [{'key','label','t','score','text'}], 'spans': [{'key','label','start','end'}]}.
    Anchors are the matched script lines; spans fill the time between anchors with the current section.
    """
    segs = [sg for sg in transcript if sg.get('words')]
    # sentence windows: this segment plus the next (a phrase often straddles whisper's segment break)
    wins = []
    for i, sg in enumerate(segs):
        txt = sg['text']
        if i + 1 < len(segs) and segs[i + 1]['start'] - sg['end'] < 1.5:
            txt += ' ' + segs[i + 1]['text']
        wseq = toks(txt)
        own = toks(sg['text'])
        wins.append((sg, wseq, set(wseq), own, set(own)))

    raw = []
    for i, (sg, wseq, wset, own, oset) in enumerate(wins):
        best, bk, bph, bpt = 0.0, None, '', None
        for k, phs in enumerate(_PH):
            for ph, pt in phs:
                sc = _coverage(pt, wset, wseq)
                if sc > best:
                    best, bk, bph, bpt = sc, k, ph, pt
        if bk is not None and best >= min_score:
            # the phrase may live mostly in the NEXT segment (the window joins two): anchor where it starts
            t = sg['start']
            if _coverage(bpt, oset, own) < 0.4 and i + 1 < len(segs):
                t = segs[i + 1]['start']
            raw.append({'i': i, 'k': bk, 'score': best, 't': t, 'text': sg['text'].strip(), 'ph': bph})

    # sections only move forward: keep the heaviest non-decreasing chain of anchors (weighted LIS)
    n = len(raw)
    best_w, prev = [0.0] * n, [-1] * n
    for i in range(n):
        best_w[i] = raw[i]['score']
        for j in range(i):
            if raw[j]['k'] <= raw[i]['k'] and best_w[j] + raw[i]['score'] > best_w[i]:
                best_w[i], prev[i] = best_w[j] + raw[i]['score'], j
    chain = []
    if n:
        i = max(range(n), key=lambda x: best_w[x])
        while i >= 0:
            chain.append(raw[i]); i = prev[i]
        chain.reverse()

    anchors = [{'key': SECTIONS[a['k']]['key'], 'label': SECTIONS[a['k']]['label'], 't': a['t'],
                'score': round(a['score'], 2), 'text': a['text'], 'phrase': a['ph']} for a in chain]
    spans = []
    for n_, a in enumerate(anchors):
        end = anchors[n_ + 1]['t'] if n_ + 1 < len(anchors) else (segs[-1]['end'] if segs else a['t'])
        if spans and spans[-1]['key'] == a['key']:
            spans[-1]['end'] = end
        else:
            spans.append({'key': a['key'], 'label': a['label'], 'start': a['t'], 'end': end})
    return {'anchors': anchors, 'spans': spans}


def section_at(spans, t):
    for sp in spans:
        if sp['start'] <= t < sp['end']:
            return sp['key']
    return None


def zone_spans(spans, zone='app'):
    """[(start, end)] of a zone the clip cutter must never touch."""
    return [(sp['start'], sp['end']) for sp in spans if BY_KEY[sp['key']].get('zone') == zone]


def label_lines(transcript, spans):
    """'(THE CLOSE)' tags for the transcript the clip picker reads."""
    out = []
    for sg in transcript:
        k = section_at(spans, sg['start'])
        out.append(('(%s) ' % BY_KEY[k]['label'].upper().rstrip('!')) if k else '')
    return out


# ---------------------------------------------------------------- on-screen cards
CARD_MAX = 10.0        # a card says where we are, then gets out of the way
CARD_MIN = 1.8
RULES = [
    # section, trigger, card text - the coaching rules Mason puts up when the moment calls for one
    {'key': 'close', 'trigger': 'silence', 'min_gap': 2.0, 'within': 15.0, 'text': 'Do Not Rush & Remain In Control'},
    {'key': 'affordability', 'trigger': 'silence', 'min_gap': 2.0, 'within': 12.0, 'text': 'Let Them Answer'},
]


def _client_runs(words, start, end, min_len=3.0):
    """Stretches where the client talks for a while - a card drops when the prospect takes over."""
    runs, cur = [], None
    for w in words:
        if w['e'] <= start or w['s'] >= end or not w.get('spk'):
            continue
        if w['spk'] != 'agent':
            if cur and w['s'] - cur[1] < 1.0:
                cur[1] = w['e']
            else:
                if cur and cur[1] - cur[0] >= min_len: runs.append(tuple(cur))
                cur = [w['s'], w['e']]
    if cur and cur[1] - cur[0] >= min_len: runs.append(tuple(cur))
    return runs


def plan_cards(det, words, start, end, spk=False):
    """Cards for one clip, in call-audio time: [(s, e, text)].

    One card per section the agent enters inside the clip, up for CARD_MAX seconds or until the next
    section, the clip's end, or the prospect taking the floor for a while. Plus the rule cards."""
    anchors = [a for a in det['anchors'] if start <= a['t'] < end - CARD_MIN and BY_KEY[a['key']].get('card')]
    # a section already running when the clip starts still deserves its label at the top
    cur = section_at(det['spans'], start)
    if cur and BY_KEY[cur].get('card') and not (anchors and anchors[0]['t'] - start < 4.0 and anchors[0]['key'] == cur):
        anchors = [{'key': cur, 'label': BY_KEY[cur]['label'], 't': start}] + anchors
    runs = _client_runs(words, start, end) if spk else []
    cards, last_key, last_t = [], None, -99.0
    for n, a in enumerate(anchors):
        if a['key'] == last_key and a['t'] - last_t < 40.0:
            continue
        s = a['t']
        e = min(s + CARD_MAX, end)
        nxt = next((b['t'] for b in anchors[n + 1:] if b['key'] != a['key']), None)
        if nxt is not None:
            e = min(e, nxt - 0.2)
        for r0, r1 in runs:                       # the prospect starts a long turn: card comes down
            if s + 2.0 < r0 < e:
                e = min(e, r0 + 0.8)
        if e - s >= CARD_MIN:
            cards.append((s, e, a['label']))
            last_key, last_t = a['key'], s
    # rule cards: a long silence right after the agent asks the closing question
    ws = sorted([w for w in words if start <= w['s'] < end], key=lambda w: w['s'])
    for rule in RULES:
        for a in anchors:
            if a['key'] != rule['key']:
                continue
            for i in range(1, len(ws)):
                gap = ws[i]['s'] - ws[i - 1]['e']
                if a['t'] < ws[i - 1]['e'] <= a['t'] + rule['within'] and gap >= rule['min_gap']:
                    s = ws[i - 1]['e'] + 0.2
                    e = min(s + 7.0, end)
                    if e - s >= CARD_MIN and not any(c[0] <= s < c[1] for c in cards):
                        cards.append((s, e, rule['text']))
                    break
            break
    cards.sort()
    # never two at once: a later card trims the one before it
    for i in range(1, len(cards)):
        if cards[i][0] < cards[i - 1][1]:
            cards[i - 1] = (cards[i - 1][0], cards[i][0] - 0.1, cards[i - 1][2])
    return [c for c in cards if c[1] - c[0] >= CARD_MIN]
