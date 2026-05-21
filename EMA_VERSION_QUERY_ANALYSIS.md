# EMA App Version Query Protocol Analysis

## Summary

The EMA app queries firmware version through BLE (Bluetooth Low Energy) communication using structured JSON messages with AES encryption. Version information is retrieved via the **GetPcsVersion** command with field names like `CV` (Current Version), `current_version`, `LV` (Latest Version), and `latest_version`.

---

## 1. Version Query Commands

### GetPcsVersion (Main Version Query)
- **Command Enum**: `AbstractC1951c.a.GetPcsVersion`
- **BLE Mode Identifier**: `"pcsVersion"` (when in BluFi mode, uses `m3606d()`)
- **HTTP Endpoint**: `""` (empty, handled locally)
- **Request DataType**: `"pcsVersion"`
- **Request MessageType**: `"get"`

### Alternative: StorageConfigInfo
- **Command Enum**: `AbstractC1951c.a.GetStorageConfigurationInfo`
- **BLE Identifier**: `"storageConfigInfo"`
- **Request DataType**: `"storageConfigInfo"`
- **Contains Version Fields**: Part of config response

---

## 2. BLE Communication Protocol

### BLE UUIDs (Device-Type Specific)

Located in `/sources/p138U0/C2009z.java`:

```java
// Default (for most PCS models)
Service UUID (f5174b):       "0000ffec-0000-1000-8000-00805f9b34fb"
Write Characteristic (f5175c): "0000ff07-0000-1000-8000-00805f9b34fb"
Notify Characteristic (f5176d): "0000ff06-0000-1000-8000-00805f9b34fb"
CCCD Descriptor (f5177e):    "00002902-0000-1000-8000-00805f9b34fb"

// EZ1_ prefix devices
Service UUID:                "016df5da-0000-1000-8000-00805f9b34fb"
Write Characteristic:        "0000ef0a-0000-1000-8000-00805f9b34fb"
Notify Characteristic:       "0000ef0b-0000-1000-8000-00805f9b34fb"

// E prefix devices
Service UUID:                "0000ffec-0000-1000-8000-00805f9b34fb"
Characteristics (same as default)

// SEM_ prefix devices
Service UUID:                "0000fffe-0000-1000-8000-00805f9b34fb"
Write Characteristic:        "0000ff0a-0000-1000-8000-00805f9b34fb"
Notify Characteristic:       "0000ff0b-0000-1000-8000-00805f9b34fb"

// LAKE_ prefix devices
Service UUID:                "0000ffed-0000-1000-8000-00805f9b34fb"
Write Characteristic:        "0000ff09-0000-1000-8000-00805f9b34fb"
Notify Characteristic:       "0000ff08-0000-1000-8000-00805f9b34fb"

// APS_A17C0 prefix devices
Service UUID:                "45100001-505d-0051-5442-508dc1425080"
Write Characteristic:        "45100002-505d-0051-5442-508dc1425080"
Notify Characteristic:       "45100003-505d-0051-5442-508dc1425080"
```

### AES Encryption/Decryption

**Encryption Keys** (defined in `C2009z.java`):
- **Key**: `"E7MiPPrs9v6i3DY3"` (f5180h)
- **IV**: `"8914934610490056"` (f5181i)
- **Algorithm**: AES/CBC/NoPadding

**Encryption Implementation** (from `AbstractC1984a.java`):
```java
public static byte[] m3801b(String str, String str2, String str3) {
    // Encrypts JSON string using AES/CBC/NoPadding
    // str = plaintext JSON
    // str2 = encryption key
    // str3 = IV (initialization vector)
    Cipher cipher = Cipher.getInstance("AES/CBC/NoPadding");
    // Pad to block size (16 bytes)
    // Return encrypted bytes
}

public static String m3800a(byte[] bArr, String str, String str2) {
    // Decrypts bytes using same AES/CBC/NoPadding
    // Returns decrypted JSON string
}
```

---

## 3. Request Format

### Method: `connectWithBlueToothLocal()` in StorageCommunication.java

**Request Structure**:
```json
{
    "T": "APS",
    "V": "01",
    "method": "get",
    "identifier": "pcsVersion",
    "type": "property",
    "params": {
        "T": "APS",
        "V": "1",
        "EID": "DEVICE_ID",
        "userId": "",
        "messagetype": "get",
        "datatype": "pcsVersion",
        "messageid": "UUID",
        "sendtime": "YYYYMMDDHHMMSS",
        "ecuid": "DEVICE_ID"
    }
}
```

**Processing Steps**:
1. Create JSON request with params
2. Encrypt JSON using `AbstractC1984a.m3801b(jsonString, key, iv)`
3. Convert encrypted bytes to hex: `hex(encryptedBytes)`
4. Send hex bytes via BLE write characteristic (f5175c)

**Code Reference** (lines 403-463 in StorageCommunication.java):
```java
public synchronized String connectWithBlueToothLocal(Map<String, Object> map, final String str) {
    map.put("T", "APS");
    map.put("V", "01");
    map.put("userId", "");
    map.put("EID", "2972245456");
    
    // Build command structure
    String str3 = (str.startsWith("set") || str.startsWith("pcsUpdate")) ? "set" : "get";
    if (str.startsWith("set/") || str.startsWith("get/")) {
        str = str.substring(4);
    }
    
    map2.put("method", str3);
    map2.put("identifier", str);
    map2.put("params", map);
    map2.put("type", "property");
    
    String jSONString = JSON.toJSONString(map2);
    
    // Encrypt and send
    AbstractC1986c.m3827s(
        hex(AbstractC1984a.m3801b(jSONString, C2009z.f5180h, C2009z.f5181i)).getBytes()
    );
    
    // Wait for response with timeout
    // Default timeout: MODULE_VERSION (35000ms)
    // Special case - gridStandards: 60000ms
}
```

---

## 4. Response Format

### Response Message Types (msgWhat values)

Located in `AbstractC1986c.java` class a (BluetoothGattCallback):

| Type | Code | Meaning | Handler |
|------|------|---------|---------|
| 9000 | Connection state | Connection established/failed | onConnectionStateChange |
| 9001 | Send failure | Write characteristic failed | mo3843j |
| 9002 | Data received | Successfully received encrypted data | mo3844k |
| 9003 | Error | BLE communication error | mo3838e |
| 9004 | Device status | WiFi connection status response | mo3836c |
| 9005 | WiFi list | WiFi scan results response | mo3835b |
| 9006 | Configure | Post-configuration response | mo3842i |
| 9007 | Connection change | Connection state changed | onConnectionStateChange |

### Response Reception & Decryption (msgWhat=9002)

**Handler** (mo3844k in AbstractC1986c.java, lines 470-490):
```java
public void mo3844k(AbstractC6430b abstractC6430b, int i6, byte[] bArr) {
    // i6 = 0 (success), else error code
    // bArr = encrypted response bytes
    
    // Decrypt response
    String decrypted = AbstractC1984a.m3800a(bArr, C2009z.f5180h, C2009z.f5181i);
    
    if (i6 == 0) {
        // Put in map with msgWhat=9002, result="success"
        map.put("msgWhat", 9002);
        map.put("result", "success");
        map.put("data", decrypted.trim());
    } else {
        map.put("msgWhat", 9002);
        map.put("result", "failed");
        map.put("data", "");
    }
    
    // Callback via AbstractC1986c.m3824p(map)
}
```

### Response Data Format

**Raw Encrypted Bytes**: 
- Received via BLE notification characteristic
- Variable length (depends on JSON payload size)

**Decrypted JSON Response**:
```json
{
    "identifier": "pcsVersion",
    "code": "200",
    "method": "get_reply",
    "company": "apsystems",
    "id": "2972245456",
    "type": "property",
    "productKey": "PCS",
    "deviceTime": 1234567890000,
    "version": "1.0",
    "deviceId": "DEVICE_ID",
    "data": {
        "CV": "X.X.X",
        "current_version": "X.X.X",
        "LV": "X.X.X",
        "latest_version": "X.X.X"
    }
}
```

### Version Field Names in Response

From `SettingFragmentByStorage.java` lines 610-624:
```java
Map<String, Object> map3 = messageData.get(0);
final String strCV = (String) map3.get("CV");           // Current Version
final String strCurrent = (String) map3.get("current_version"); // Current Version (alternate)
final String strLV = (String) map3.get("LV");           // Latest Version
final String strLatest = (String) map3.get("latest_version");   // Latest Version (alternate)
```

**Field Mapping**:
| Field | Meaning |
|-------|---------|
| `CV` | Current Version (short form) |
| `current_version` | Current Version (long form) |
| `LV` | Latest Version (short form) |
| `latest_version` | Latest Version (long form) |

---

## 5. Response Parsing Pipeline

### Step 1: dealReply() Method (line 114 in StorageCommunication.java)

```java
private StorageCommunicationResolve dealReply(
    String str,           // JSON response string
    boolean z6,
    boolean z7,
    Map<String, Object> map,
    Map<String, Object> map2,
    String str2,
    String str3
) {
    StorageCommunicationResolve result = new StorageCommunicationResolve();
    
    if (str.isEmpty()) {
        setConnectedStatus("false");
        return result;
    }
    
    // Parse JSON string to List<Map>
    List<Map<String, Object>> listM21036s = AbstractC7997o.m21036s(str);
    
    // Check for error codes
    if ("8".equals(String.valueOf(listM21036s.get(0).get("code")))) {
        return reSend(...);  // Retry on error code 8
    }
    
    if ("9999".equals(String.valueOf(listM21036s.get(0).get("code")))) {
        result.setErrorCode("9999");
        return result;
    }
    
    // Success check
    if ("0".equals(String.valueOf(listM21036s.get(0).get("result")))) {
        result.setErrorCode("1");
    } else {
        result.setErrorCode(String.valueOf(listM21036s.get(0).get("code")));
    }
    
    result.setCommData(listM21036s);
    setConnectedStatus("true");
    return result;
}
```

### Step 2: Response Data Extraction

From `SettingFragmentByStorage.java` lines 609-624:

```java
StorageCommunicationResolve response = storageCommunication.sendNewCommand(
    map2,    // BLE params
    map,     // request metadata
    AbstractC1951c.a.GetPcsVersion
);

// Determine which data field to use based on model
List<Map<String, Object>> messageData = 
    "1".equals(BaseApplication.m10427g().getModel()) 
        ? response.getMessageData() 
        : response.getReplyData();

if (messageData == null || messageData.isEmpty()) {
    // Handle error
} else {
    Map<String, Object> map3 = messageData.get(0);
    String cv = (String) map3.get("CV");
    String currentVersion = (String) map3.get("current_version");
    String lv = (String) map3.get("LV");
    String latestVersion = (String) map3.get("latest_version");
}
```

---

## 6. Error Codes and Retry Logic

### Error Handling (dealReply method)

**Error Codes**:
- `"0"` = Success (result field)
- `"8"` = Retry condition (triggers reSend())
- `"9999"` = Fatal error (authentication/session fail)
- Other codes = Error condition with code in response

### Retry Logic

**reSend() Method** (lines 270-305 in StorageCommunication.java):
- Triggered when error code = 8
- Attempts to re-authenticate user
- Sends login request to verify credentials
- Re-executes original command if auth succeeds

**Timeout Handling**:
- Default timeout: 35000ms (35 seconds)
- Special cases:
  - `gridStandards`: 60000ms (60 seconds)
  - Some device queries: `ModuleDescriptor.MODULE_VERSION` (~35000ms)

---

## 7. Version Query Flow Diagram

```
┌─────────────────────────────────────────────────┐
│ 1. Create Version Query Request                 │
│    - Set datatype="pcsVersion"                  │
│    - Set messagetype="get"                      │
│    - Add params: T, V, EID, etc.               │
└─────────────────────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────┐
│ 2. Encrypt Request                              │
│    - JSON → serialize to string                 │
│    - Use AES/CBC/NoPadding with:               │
│      Key: "E7MiPPrs9v6i3DY3"                  │
│      IV: "8914934610490056"                    │
│    - Convert to hex string                      │
└─────────────────────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────┐
│ 3. Send via BLE                                 │
│    - Write to characteristic (f5175c)           │
│    - Payload: hex(encrypted bytes)             │
│    - Max MTU: 500 bytes                        │
└─────────────────────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────┐
│ 4. Receive Notification                         │
│    - Listen on notify characteristic (f5176d)   │
│    - Receive encrypted bytes                    │
│    - Generate msgWhat=9002 event                │
└─────────────────────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────┐
│ 5. Decrypt Response                             │
│    - Use same AES key/IV                       │
│    - Decrypt bytes to JSON string              │
│    - Trim and parse JSON                       │
└─────────────────────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────┐
│ 6. Parse Response                               │
│    - Extract code field                        │
│    - Check for errors (8, 9999)                │
│    - Extract version fields: CV, LV            │
│    - Return in Map with getMessageData()       │
└─────────────────────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────┐
│ 7. Version Fields Available                     │
│    - CV or current_version = Current           │
│    - LV or latest_version = Latest             │
└─────────────────────────────────────────────────┘
```

---

## 8. Alternative Version Query: StorageConfigInfo

### Command Details

```java
// In StorageCommunication.java line 915
public StorageCommunicationResolve getStorageConfigInfo(Map<String, Object> map) {
    HashMap map2 = new HashMap();
    map2.put("T", "APS");
    map2.put("V", "1");
    map2.put("EID", BaseApplication.m10427g().getCurrentDeviceId());
    map.put("messagetype", "get");
    map.put("datatype", "storageConfigInfo");
    map.put("messagedata", map2);
    
    String strM3604a = AbstractC1951c.m3604a(AbstractC1951c.a.GetStorageConfigurationInfo);
    String strSendCommand = sendCommand(-1, -1, -1, true, false, map2, map, 
                                        strM3604a, "api/remote/set/storage/publishMqttMessage");
    
    return dealReply(strSendCommand, true, false, map2, map, strM3604a, 
                     "api/remote/set/storage/publishMqttMessage");
}
```

**Usage**: Returns full device configuration including version information

---

## 9. Device Model Detection

Device types are detected by model string prefix and configure appropriate BLE UUIDs:

```java
C2009z.m3951o(String deviceModel) {
    if (deviceModel.startsWith("EZ1_")) {
        // Use EZ1 UUIDs
    } else if (deviceModel.startsWith("E")) {
        // Use E series UUIDs
    } else if (deviceModel.startsWith("SEM_")) {
        // Use SEM UUIDs
    } else if (deviceModel.startsWith("LAKE_")) {
        // Use LAKE UUIDs
    } else if (deviceModel.startsWith("APS_A17C0")) {
        // Use APS_A17C0 UUIDs
    } else if (deviceModel.startsWith("PCS")) {
        // Use default/PCS UUIDs
    }
}
```

---

## 10. Implementation Summary

### Key Classes
- **StorageCommunication.java**: Main communication orchestrator
- **AbstractC1986c.java**: BLE callback handler and message types
- **AbstractC1984a.java**: AES encryption/decryption
- **C2009z.java**: BLE UUIDs and encryption keys
- **AbstractC1951c.java**: Command enum definitions

### Key Methods
- `sendNewCommand()`: Initiates version query
- `connectWithBlueToothLocal()`: Handles BLE communication
- `dealReply()`: Processes response with error handling
- `mo3844k()`: Receives and decrypts encrypted data
- `getStorageConfigInfo()`: Alternative config retrieval

### Response Fields
- **`CV`**: Current firmware version
- **`LV`**: Latest available firmware version
- **`code`**: Response code (200 = success)
- **`identifier`**: "pcsVersion" for version queries
- **`method`**: "get_reply" for responses

---

## 11. Key Implementation Details

### Message Structure Fields

**Request Metadata** (outer map):
- `T`: "APS" (tag)
- `V`: "01" (version)
- `method`: "get" or "set"
- `identifier`: "pcsVersion"
- `type`: "property"

**Request Params** (inner params):
- `T`: "APS"
- `V`: "1"
- `EID`: Device ID
- `messagetype`: "get"
- `datatype`: "pcsVersion"
- `messageid`: UUID (unique per request)
- `sendtime`: Timestamp
- `ecuid`: Device ID

### Response Structure
- **identifier**: Matches request identifier
- **code**: "200" for success
- **method**: "get_reply"
- **data**: Contains version fields (CV, LV, etc.)
- **deviceTime**: Server timestamp
- **productKey**: Device type (PCS, SEM, etc.)

---

## 12. Special Cases & Fallbacks

### Model-Specific Response Data Selection
```java
// Different response fields based on model
if ("1".equals(model)) {
    versionData = response.getMessageData();
} else {
    versionData = response.getReplyData();
}
```

### Retry Conditions
- **Error Code 8**: Triggers authentication retry
- **Error Code 9999**: Session failure, no retry
- **Timeout**: After 35 seconds (special: 60s for gridStandards)

### Connection Fallbacks
- **Online mode**: Uses HTTP endpoint
- **BluFi mode**: Uses BLE (primary)
- **Local mode**: Uses TCP connection with timeout handling

