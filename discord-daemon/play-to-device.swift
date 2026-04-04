#!/usr/bin/env swift
// play-to-device.swift — Play audio file to a specific CoreAudio output device
// Usage: swift play-to-device.swift <device-name-or-index> <audio-file>
//        swift play-to-device.swift --list
//
// Supports: WAV, PCM (raw s16le 48kHz mono), CAF, AIFF

import AVFoundation
import CoreAudio
import Foundation

// List all audio output devices
func listDevices() {
    var propAddress = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDevices,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var dataSize: UInt32 = 0
    AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &propAddress, 0, nil, &dataSize)
    let count = Int(dataSize) / MemoryLayout<AudioDeviceID>.size
    var devices = [AudioDeviceID](repeating: 0, count: count)
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &propAddress, 0, nil, &dataSize, &devices)

    for (idx, device) in devices.enumerated() {
        // Check if device has output channels
        var streamAddress = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyStreams,
            mScope: kAudioObjectPropertyScopeOutput,
            mElement: kAudioObjectPropertyElementMain
        )
        var streamSize: UInt32 = 0
        let status = AudioObjectGetPropertyDataSize(device, &streamAddress, 0, nil, &streamSize)
        if status != noErr || streamSize == 0 { continue }

        var nameAddress = AudioObjectPropertyAddress(
            mSelector: kAudioObjectPropertyName,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var name: CFString = "" as CFString
        var nameSize = UInt32(MemoryLayout<CFString>.size)
        AudioObjectGetPropertyData(device, &nameAddress, 0, nil, &nameSize, &name)
        print("\(idx): \(name) (id: \(device))")
    }
}

// Find device by name
func findDevice(named target: String) -> AudioDeviceID? {
    var propAddress = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDevices,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var dataSize: UInt32 = 0
    AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &propAddress, 0, nil, &dataSize)
    let count = Int(dataSize) / MemoryLayout<AudioDeviceID>.size
    var devices = [AudioDeviceID](repeating: 0, count: count)
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &propAddress, 0, nil, &dataSize, &devices)

    for device in devices {
        var nameAddress = AudioObjectPropertyAddress(
            mSelector: kAudioObjectPropertyName,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var name: CFString = "" as CFString
        var nameSize = UInt32(MemoryLayout<CFString>.size)
        AudioObjectGetPropertyData(device, &nameAddress, 0, nil, &nameSize, &name)
        if (name as String).lowercased().contains(target.lowercased()) {
            return device
        }
    }
    return nil
}

// Main
let args = CommandLine.arguments
if args.count < 2 {
    print("Usage: play-to-device <device-name> <audio-file>")
    print("       play-to-device --list")
    exit(1)
}

if args[1] == "--list" {
    listDevices()
    exit(0)
}

guard args.count >= 3 else {
    print("Error: need device name and audio file")
    exit(1)
}

let deviceName = args[1]
let filePath = args[2]

guard let deviceId = findDevice(named: deviceName) else {
    print("Error: device '\(deviceName)' not found")
    listDevices()
    exit(1)
}

let url = URL(fileURLWithPath: filePath)
guard FileManager.default.fileExists(atPath: filePath) else {
    print("Error: file not found: \(filePath)")
    exit(1)
}

do {
    let engine = AVAudioEngine()

    // Set output device
    var outputDevice = deviceId
    let outputNode = engine.outputNode
    let audioUnit = outputNode.audioUnit!
    AudioUnitSetProperty(
        audioUnit,
        kAudioOutputUnitProperty_CurrentDevice,
        kAudioUnitScope_Global,
        0,
        &outputDevice,
        UInt32(MemoryLayout<AudioDeviceID>.size)
    )

    let playerNode = AVAudioPlayerNode()
    engine.attach(playerNode)

    let audioFile = try AVAudioFile(forReading: url)
    let format = audioFile.processingFormat

    engine.connect(playerNode, to: engine.mainMixerNode, format: format)
    try engine.start()

    let semaphore = DispatchSemaphore(value: 0)

    playerNode.scheduleFile(audioFile, at: nil) {
        // Signal on a global queue since there's no main run loop in CLI
        DispatchQueue.global().asyncAfter(deadline: .now() + 0.2) {
            semaphore.signal()
        }
    }
    playerNode.play()

    let duration = Double(audioFile.length) / audioFile.processingFormat.sampleRate
    print("Playing \(filePath) → \(deviceName) (device \(deviceId), \(String(format: "%.1f", duration))s)...")

    // Wait with a max timeout based on audio duration + buffer
    let maxWait = DispatchTime.now() + duration + 2.0
    let result = semaphore.wait(timeout: maxWait)

    playerNode.stop()
    engine.stop()

    if result == .timedOut {
        print("Done (timeout fallback).")
    } else {
        print("Done.")
    }
} catch {
    print("Error: \(error.localizedDescription)")
    exit(1)
}
