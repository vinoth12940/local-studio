// Plain-node guard for frontend/voice/turn.mjs: end-of-turn and backchannel decisions on labeled phrases (ported from
// the voice POC's eval). Run by macos/build.sh:  node macos/check_voice_ui.mjs
import { endpointWait, isBackchannel, looksLikeOwnVoice, takeSentences } from "../frontend/voice/turn.mjs";
import assert from "node:assert/strict";

// [text, 1 = finished (answer now), 0 = mid-thought (wait)]
const TURNS = [
  ['what is the weather in tokyo right now', 1], ["what's the capital of france", 1], ['tell me a joke', 1], ['how are you doing today', 1],
  ['i just got the job offer i wanted', 1], ['can you search for the latest apple news', 1], ['hello', 1], ['yes', 1], ['no stop', 1], ['thanks', 1],
  ['that is interesting tell me more', 1], ['I think so', 1], ['I love you', 1], ['what time is it', 1], ['remind me to call mom tomorrow', 1],
  ['open my latest visa document', 1], ['okay', 1], ['what do you think about it', 1], ['do you know what I mean', 1], ['why', 0],
  ['tell me about my', 0], ['I want to know about the', 0], ['so I was thinking that', 0], ['can you find the file and', 0], ['what is the', 0],
  ['I was going to say but', 0], ['the problem is because', 0], ['um', 0], ['so', 0], ['I need to', 0], ['well I think if', 0],
  ['can you tell me how', 0], ['search for something with', 0], ['remind me about the meeting with my', 0], ['and then', 0], ['I would like to', 0],
];
const BACKCH = [['yeah', 1], ['uh-huh', 1], ['mm hmm', 1], ['okay', 1], ['I see', 1], ['got it', 1], ['oh wow', 1], ['right', 1], ['really', 1], ['sure', 1],
  ['wait', 0], ['stop', 0], ['no', 0], ['actually I meant tokyo', 0], ['what about tomorrow', 0], ['hold on', 0], ['can you repeat that', 0], ['I see a problem here', 0],
  ['yes but', 0], ['got it wrong', 0], ['tell me more', 0], ['cancel that', 0]];
const HELD = [
  ["what's the best way to learn guitar", 1], ['play some jazz music', 1], ["I'm feeling a bit tired today", 1], ['can you summarize this article for me', 1],
  ['who won the game last night', 1], ['turn off the lights please', 1], ['how far is the moon from earth', 1], ['that sounds great thank you', 1],
  ["I don't think that's right", 1], ['give me a minute', 1], ['set a timer for ten minutes', 1], ['what are you looking at', 1],
  ['do you remember what I told you yesterday', 1], ['no I meant the other one', 1], ['I really enjoyed that', 1], ['good morning', 1],
  ['I was wondering if you could', 0], ['my friend told me that', 0], ['the thing about this is that', 0], ['can you help me with', 0],
  ["I'm trying to figure out how to", 0], ['so the reason I called is', 0], ['what I mean is that', 0], ['and also', 0],
  ['remind me to buy milk and', 0], ['I need you to find the', 0], ['let me think about', 0], ['it depends on whether', 0],
];
const HELD2 = [
  ['what time does the store close', 1], ['I had a great day at work', 1], ['can you read my last message', 1], ['I think we should go', 1],
  ['show me pictures of mountains', 1], ['how do I get to the airport', 1], ['it was really nice to meet you', 1], ['wake me up at seven', 1],
  ['where are you from', 1], ['what are you thinking about', 1], ['I changed my mind', 1], ['that makes sense', 1], ['do you like music', 1], ['good evening', 1],
  ['what is the weather like in paris today', 1], ['I need a recipe for dinner', 1],
  ['I was going to ask you about', 0], ['the main problem with that is', 0], ['could you please check whether', 0], ['I wanted to tell you that', 0],
  ['if you could', 0], ['and my sister said', 0], ['I have a question about the', 0], ['so basically what happened was', 0],
  ['can you look up the price of', 0], ['we are planning a trip to', 0], ['the idea is to', 0], ['please send it to my', 0],
];

const score = (rows, f) => rows.filter(([t, y]) => (f(t) ? 1 : 0) === y).length;
const done = (t) => endpointWait(t)[0] <= 400;
const s1 = score(TURNS, done), s2 = score(HELD, done), s3 = score(HELD2, done), b = score(BACKCH, isBackchannel);
assert.equal(s1, TURNS.length, "end-of-turn regressed on the tuning set");
assert.equal(s2, HELD.length, "end-of-turn regressed on the second set");
assert.ok(s3 >= 26, `end-of-turn on the fresh set fell to ${s3}/${HELD2.length} (was 26)`);
assert.equal(b, BACKCH.length, "backchannel detection regressed");
// Streaming text is cut into speakable pieces; the first may end at a clause so speech starts sooner.
assert.deepEqual(takeSentences("The ocean covers most of the planet surface, matey — that is", false, true)[0], ["The ocean covers most of the planet surface,"]);
assert.deepEqual(takeSentences("Ahoy, matey! Aye", false, true), [["Ahoy, matey!"], "Aye"]);
assert.deepEqual(takeSentences("It costs 3.5 dollars", false, true)[0], [], "a decimal point is not a sentence end");
assert.deepEqual(takeSentences("Last words", true, false)[0], ["Last words"]);
// Its own voice heard back is recognised; a new question is not.
assert.ok(looksLikeOwnVoice("sitting around seventy", "Bitcoin is sitting around seventy nine thousand"));
assert.ok(!looksLikeOwnVoice("what about Tokyo", "Bitcoin is sitting around seventy nine thousand"));
console.log(`   voice turn checks OK (end-of-turn ${s1}/${TURNS.length}, ${s2}/${HELD.length}, ${s3}/${HELD2.length}; backchannels ${b}/${BACKCH.length})`);
