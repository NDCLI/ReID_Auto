# Model sources

Runtime models are cached under `%LOCALAPPDATA%\ReIDAutoOSNet\models` by
`install.bat`; large model binaries are deliberately not committed to Git.

This build deliberately has **no TransReID**. Every body model below takes raw
BGR pixels in the 0-255 range at 256x128 and returns a 256-D embedding, which is
exactly what `_OpenVINOEmbeddingModel.extract()` supplies. Do not add a model
whose preprocessing (RGB order, `/255`, mean/std) is not already baked into its
graph — the loader applies none of it, and a mismatch produces silently wrong
embeddings rather than an error.

## person-reidentification-retail-0277

- Source: Intel Open Model Zoo 2023.0, FP16 IR
- Documentation: https://docs.openvino.ai/2023.3/omz_models_model_person_reidentification_retail_0277.html
- `reid_0277.xml` SHA-256: `82039B1BB986231C50345550E59DEF94E63579998E9CDDBD1A312F293EB87729`
- `reid_0277.bin` SHA-256: `6BB69E09D07733CC3896B29081D2627F92DB7B9AACD50F1A8C3DF491CB10F1B7`

## person-reidentification-retail-0286

Added in this variant as the replacement for TransReID.

- Source: Intel Open Model Zoo 2023.0, FP16 IR
- Documentation: https://docs.openvino.ai/2023.3/omz_models_model_person_reidentification_retail_0286.html
- Architecture: OSNet backbone with Linear Context Transform (LCT) blocks
- Input: `1, 3, 256, 128` NCHW, **BGR channel order**, no normalization
- Output: `reid_embedding`, `1, 256`, compared with cosine similarity
- Reported accuracy on Market-1501: rank@1 94.8%, mAP 83.7%
- Download: `https://storage.openvinotoolkit.org/repositories/open_model_zoo/2023.0/models_bin/1/person-reidentification-retail-0286/FP16/person-reidentification-retail-0286.{xml,bin}`
- `reid_0286.xml` SHA-256: `CC547E622D8AEABF97938213F68171BF59AC7D01C910CEADFEA984AA96E5D1B5`
- `reid_0286.bin` SHA-256: `D06BEB9E2B2661D42B233F64EF623F3518FC46DC11F6B7DD53CDA8962D9D1BD9`

### Why not FastReID AGW R50-IBN

AGW R50-IBN was evaluated first because it is a genuinely different
(ResNet/IBN) architecture. It ships only as a PyTorch checkpoint
(`market_agw_R50-ibn.pth`), so using it requires exporting to ONNX with
BGR-to-RGB and ImageNet mean/std folded into the graph. That export could not be
performed here — the installed `torch` fails to load (`WinError 1114`,
`c10.dll`) — and shipping an unvalidated export risks the silent-wrong-embedding
failure described above. 0286 was selected instead: it is an official
prebuilt IR with the same input contract as the models already in use.

## Face detection and face re-identification

- Source: Intel Open Model Zoo 2023.0, FP16 IR
- Detection model: `face-detection-retail-0005`
- Recognition model: `face-reidentification-retail-0095`
- `face-detection-retail-0005.xml` SHA-256: `C5CC97916A594EDE031BD2BCA91B4FFC12ABD4597B307EFEDF51FE4EDD8F314A`
- `face-detection-retail-0005.bin` SHA-256: `21CC37045583739A5ED1B1BD1BAC44EAFA8261708E07A8BDB9A2D044A132CC26`
- `face-reidentification-retail-0095.xml` SHA-256: `CE53D2C9C08C0BD1C1660FB8A5B6D0E3E4EC19EB92F1036D2D83A85E83082DCE`
- `face-reidentification-retail-0095.bin` SHA-256: `241229CA3D206321868D46CE74A3C0B06C49CEA58DB7DC70B2E842FF287545D1`