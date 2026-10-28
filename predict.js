document.addEventListener("DOMContentLoaded", () => {
    const symptomInput = document.getElementById("symptomInput");
    if (symptomInput) {
        symptomInput.addEventListener("input", analyzeSymptoms);
    }

    const otherInput = document.getElementById("otherInput");
    if (otherInput) {
        otherInput.addEventListener("input", analyzeOtherInput);
    }
});

// =============================
// SELECT SYMPTOM BUTTON
// =============================
function selectEmergency(element, name) {
    // hide OTHER input
    const otherBox = document.getElementById("otherInputBox");
    if (otherBox) otherBox.style.display = "none";

    // Unselect other buttons
    document.querySelectorAll('.symptom-btn').forEach(btn => {
        if (btn !== element) btn.classList.remove('selected');
    });

    const isSelected = element.classList.toggle("selected");
    const textarea = document.getElementById("symptomInput");

    textarea.value = isSelected ? name : "";

    if (isSelected) {
    analyzeSymptoms();

    // 🔥 ADD THIS BELOW
    const resultBox = document.getElementById("result");

    resultBox.style.display = "block";
    resultBox.style.border = "3px solid red";

    resultBox.scrollIntoView({ behavior: "smooth" });
} else {
        document.getElementById("result").style.display = "none";
    }
}

// =============================
// SHOW OTHER INPUT
// =============================
function showOtherInput(element) {
    const box = document.getElementById("otherInputBox");

    document.querySelectorAll(".symptom-btn").forEach(btn => {
        if (btn !== element) btn.classList.remove("selected");
    });

    element.classList.toggle("selected");
    box.style.display = element.classList.contains("selected") ? "block" : "none";
    
    document.getElementById("symptomInput").value = "";
    document.getElementById("result").style.display = "none";
}

// =============================
// ANALYZE OTHER INPUT
// =============================
function analyzeOtherInput() {
    const input = document.getElementById("otherInput").value.toLowerCase().trim();
    const resultBox = document.getElementById("result");

    document.getElementById("symptomInput").value = input;

    if (!input) {
        resultBox.style.display = "none";
        return;
    }

    showResult(input, resultBox);
}

// =============================
// ANALYZE TEXTAREA
// =============================
function analyzeSymptoms() {
    const text = document.getElementById("symptomInput").value.toLowerCase().trim();
    const resultBox = document.getElementById("result");

    if (!text) {
        resultBox.style.display = "none";
        return;
    }

    showResult(text, resultBox);
}

// =============================
// SHOW RESULT
// =============================
function showResult(text, resultBox) {
    const emergencyKeywords = [
        "cardiac arrest", "heart attack", "stroke", "breathing", "unconscious",
        "bleeding", "blood", "wound", "cut", "burn", "seizure", "fits", "convulsions", "poison", "snake bite", "venom", "toxic", "overdose",
        "allergy", "chest pain", "cardiac", "face droop", "spine injury", "fracture", "head injury", "spinal",
        "drowning", "electric shock", "severe pain", "severe burn", "severe injury","accidents",
        "severe bleeding", "accident", "injury", "collapse", "fainting", "faint", "choking",
        "vomiting blood", "loss of consciousness", "sudden weakness", "maternity", "labor", "pregnancy"
    ];

    const isEmergency = emergencyKeywords.some(k => text.includes(k));

    resultBox.style.display = "block";

    if (isEmergency) {
        resultBox.style.backgroundColor = "#fff5f5";
        resultBox.style.borderLeft = "5px solid #dc3545";
        resultBox.innerHTML = `
            <h3 style="color:#dc3545;">⚠️ Emergency Detected</h3>
            <a href="/booking" class="book-ambulance-btn" style="background-color: #dc3545;">🚑 Book Ambulance Now</a>
            ${renderPrecautions(text)}
        `;
        playAlertSound();
    } else {
        resultBox.style.backgroundColor = "#f0fdf4";
        resultBox.style.borderLeft = "5px solid #16a34a";
        resultBox.innerHTML = `
            <h3 style="color:#16a34a;">✅ No Immediate Emergency</h3>
            <a href="/booking" class="book-ambulance-btn" style="background-color: #16a34a;">🚑 Book Ambulance (Optional)</a>
            ${renderPrecautions(text)}
        `;
    }
    
    // Auto-scroll to result box smoothly
    resultBox.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// =============================
// PLAY ALERT SOUND
// =============================
function playAlertSound() {
    // Basic beep sound using AudioContext
    try {
        const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        const oscillator = audioCtx.createOscillator();
        const gainNode = audioCtx.createGain();
        
        oscillator.connect(gainNode);
        gainNode.connect(audioCtx.destination);
        
        oscillator.type = 'square';
        oscillator.frequency.value = 800; // Hz
        gainNode.gain.setValueAtTime(0.1, audioCtx.currentTime); // Low volume
        
        oscillator.start();
        setTimeout(() => {
            oscillator.stop();
        }, 300); // 300ms beep
    } catch (e) {
        console.log("Audio alert not supported or blocked by browser policy");
    }
}


// =============================
// PRECAUTIONS FUNCTION
// =============================
function buildPrecautions(text) {
    const value = (text || "").toLowerCase();

    // 🛑 CARDIAC ARREST
    if (value.includes("cardiac arrest")||value.includes("not breathing")||value.includes("no pulse")||value.includes("heart stopped")) {
        return [
            "🚨 Call for an ambulance immediately!",
            "🫀 Start CPR pushing hard and fast in the center of the chest",
            "⚡ Use an AED if one is available nearby",
            "🚫 Do not stop CPR until help arrives or the person wakes up"
        ];
    }

    // ❤️ HEART ATTACK / CHEST PAIN
    if (value.includes("heart attack") || value.includes("chest") || value.includes("cardiac")||value.includes("chest pain")) {
        return [
            "🪑 Have the person sit down, rest, and try to keep calm",
            "👔 Loosen any tight clothing around the neck and chest",
            "💊 Give aspirin if they are not allergic and conscious",
            "🚑 Call ambulance immediately if pain lasts more than a few minutes"
        ];
    }

    // 🧠 STROKE / SUDDEN NEUROLOGICAL ISSUES
    if (value.includes("stroke") || value.includes("weakness") || value.includes("face droop") || value.includes("speaking") || value.includes("confusion") || value.includes("numbness") || value.includes("vision") || value.includes("balance") || value.includes("walking") || value.includes("dizziness")) {
        return [
            "⚠️ Think FAST: Face drooping, Arm weakness, Speech difficulty, Time to call!",
            "🚑 Call an ambulance immediately and note the time symptoms started",
            "🚫 Do not give them anything to eat or drink",
            "🛏 Lay them down safely and keep them calm"
        ];
    }

    // 😮‍💨 BREATHING / CHOKING
    if (value.includes("breath") || value.includes("choking")||value.includes("asphyxiation")||value.includes("airway")||value.includes("suffocating")||value.includes("strangulation")) {
        return [
            "🪑 Sit them upright to help them breathe",
            "🌬 Help them use an inhaler if they have one",
            "💥 If choking: give up to 5 back blows and 5 abdominal thrusts",
            "🚑 Call an ambulance immediately if they cannot breathe"
        ];
    }

    // 😵 UNCONSCIOUS / FAINTING / COLLAPSE
    if (value.includes("unconscious") || value.includes("collapse") || value.includes("faint") || value.includes("not responding") || value.includes("loss of consciousness")||value.includes("unresponsive")||value.includes("passed out")||value.includes("fainted")) {
        return [
            "🛌 Place the person in the recovery position (on their side)",
            "👀 Check if they are breathing normally",
            "🚫 Do not force water or food into their mouth",
            "🚑 Call an ambulance immediately"
        ];
    }

    // 🩸 BLEEDING
    if (value.includes("bleed") || value.includes("blood") || value.includes("cut") || value.includes("wound")||value.includes("laceration")||value.includes("hemorrhage")||value.includes("severe bleeding")||value.includes("profuse bleeding")||value.includes("arterial bleeding")) {
        return [
            "🩸 Apply firm, direct pressure to the wound using a clean cloth",
            "⬆ Keep the injured area raised above the heart if possible",
            "🩹 Keep pressing until the bleeding stops",
            "🧴 Clean wound with antiseptic",
            "🚑 Call for help if the bleeding is severe or won't stop"

        ];
    }

    // 🔥 BURNS
    if (value.includes("burn")||value.includes("scald")||value.includes("thermal")||value.includes("chemical")||value.includes("electrical burn")||value.includes("severe burn")||value.includes("burning")||value.includes("fire accident")) {
        return [
            "🚿 Cool the burn under gently running cool water for at least 10 minutes",
            "🚫 DO NOT use ice, butter, or creams on the burn",
            "🩹 Cover lightly with a clean plastic film or sterile dressing",
            "🚑 Seek medical help for severe or large burns"
        ];
    }

    // 🧠 SEIZURE / FITS
    if (value.includes("seizure") || value.includes("fit") || value.includes("convulsion")||value.includes("epilepsy")||value.includes("seizing")||value.includes("convulsing")||value.includes("shaking uncontrollably")) {
        return [
            "🛡️ Move dangerous objects away to prevent injury",
            "🛌 Place something soft under their head",
            "🚫 DO NOT hold them down or put anything in their mouth",
            "🕒 Time the seizure and call an ambulance if it lasts over 5 minutes"
        ];
    }

    // 💊 POISONING / SNAKE BITE
    if (value.includes("poison") || value.includes("snake") || value.includes("venom") || value.includes("toxic") || value.includes("overdose")||value.includes("snake bite")||value.includes("envenomation")||
    value.includes("poisoning")||value.includes("toxic ingestion")||value.includes("overdose")||value.includes("snake bite")) {
        return [
            "🛑 Keep the person completely still and calm",
            "🚫 DO NOT force them to vomit",
            "🚫 DO NOT try to suck the venom out or cut the bite",
            "🚑 Call an ambulance and poison control immediately"
        ];
    }

    // 🤧 SEVERE ALLERGY
    if (value.includes("allerg") || value.includes("rash") || value.includes("itching") || value.includes("swelling")||value.includes("hives") || value.includes("anaphylaxis") || value.includes("allergic reaction") || value.includes("allergy attack")) {
        return [
            "💉 Help them use an EpiPen (auto-injector) if they have one",
            "🪑 Sit them up if they are struggling to breathe",
            "🛌 Lay them flat with legs raised if they feel dizzy",
            "🚑 Call an ambulance immediately"
        ];
    }

    // 🦴 SPINE / HEAD / FRACTURE
    if (value.includes("spine") || value.includes("spinal") || value.includes("fracture") || value.includes("head") || value.includes("neck")||value.includes("spinal injury")||value.includes("head injury")||value.includes("neck injury")||value.includes("broken bone")||value.includes("bone fracture")) {
        return [
            "🛑 DO NOT move the person unless they are in immediate danger",
            "🧱 Hold their head and neck completely still",
            "🛌 Keep them resting in the position you found them",
            "🚑 Call an ambulance immediately"
        ];
    }

    // 🌊 DROWNING
    if (value.includes("drown")||value.includes("Drowning")||value.includes("submersion")||value.includes("near drowning")||value.includes("water inhalation")||value.includes("asphyxiation in water")||value.includes("drowning incident")) {
        return [
            "🌊 Get the person out of the water immediately",
            "👀 Check for breathing and clear their airway",
            "🫀 Start CPR immediately if they are not breathing",
            "🚑 Call for an ambulance as soon as possible"
        ];
    }

    // ⚡ ELECTRIC SHOCK
    if (value.includes("electric")||value.includes("shock")||value.includes("electrocution")||value.includes("current")||value.includes("electrical injury")||value.includes("electric shock injury")||value.includes("electrocution incident")) {
        return [
            "⚡ Turn off the power source first before touching the person",
            "🚫 DO NOT touch the person if they are still attached to the power",
            "🫀 Check for breathing and start CPR if needed",
            "🚑 Call an ambulance immediately"
        ];
    }

    // 🤰 MATERNITY
    if (value.includes("maternity") || value.includes("labor") || value.includes("pregnancy")) {
        return [
            "🤰 Keep the mother calm and comfortable",
            "🛌 Have her lie on her left side",
            "🧘 Encourage slow, deep breaths",
            "🚑 Call an ambulance immediately"
        ];
    }

    // 💥 SEVERE PAIN / VOMITING / DIARRHEA
    if (value.includes("pain") || value.includes("vomit") || value.includes("diarrhea")||value.includes("blood discharge")) {
        return [
            "🪑 Rest in the most comfortable position",
            "💧 Take small sips of water to stay hydrated",
            "🚫 Do not take random painkillers without doctor advice",
            "🚑 Call a doctor or ambulance if symptoms are sudden and severe"
        ];
    }

    // 🤕 ACCIDENT / EMERGENCY
    if (value.includes("accidents") || value.includes("injury") || value.includes("emergency")||value.includes("collapse")||value.includes("accident")||value.includes("fainting")||value.includes("faint")||value.includes("choking")||value.includes("vomiting blood")||value.includes("loss of consciousness")||value.includes("sudden weakness")) {
        return [
            "🛑 Ensure the area is safe before approaching",
            "👀check consciousness and breathing",
            "🫀start CPR if not breathing [only if trained]",
            "🛌 Do not move the injured person unless absolutely necessary",
            "🩸 Check for severe bleeding and apply pressure",
            "🚑 Call for an ambulance immediately"
        ];
    }

    // 🤒 FEVER
    if (value.includes("fever")) {
        return [
            "🤒 Rest well and avoid heavy physical activity",
            "💧 Drink plenty of water and clear fluids",
            "👕 Wear light, loose-fitting clothing",
            "👉 Consult a doctor if fever is very high or lasts over 3 days"
        ];
    }

    // 🤕 HEADACHE
    if (value.includes("headache")||value.includes("migraine")) {
        return [
            "🛏 Rest in a quiet, dark room",
            "💧 Drink a glass of water",
            "🚫Avoid exposure to screens and noise",
            "🧊 Apply a cold compress to your forehead",
            "👉 Consult a doctor if the headache is sudden and extremely severe"
        ];
    }

    // 🤧 COLD / COUGH
    if (value.includes("cold") || value.includes("cough")) {
        return [
            "💧 Drink warm water or tea",
            "🛏 Get plenty of rest and stay warm",
            "🍯 Take honey to soothe the throat (if not allergic)",
            "👉 See a doctor if symptoms last more than a week"
        ];
    }

    // DEFAULT
    return [
        "😌 Stay calm and rest",
        "🧼 Keep your hands and surrounding area clean",
        "🚫 Do not take unprescribed medicines",
        "🚑 Contact a healthcare professional if symptoms worsen"
    ];
}


// =============================
// RENDER PRECAUTIONS
// =============================
function renderPrecautions(text) {

    const list = buildPrecautions(text);

    let html = `<div class="precautions"><h4>🩺 What to do:</h4><ul>`;

    list.forEach(item => {
        html += `<li>${item}</li>`;
    });

    html += `</ul></div>`;

    return html;
}
