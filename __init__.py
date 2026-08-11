class LoopLastFrame:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"images": ("IMAGE",)}}

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "last"
    CATEGORY = "Loop"

    def last(self, images):
        # ComfyUI IMAGE tensors are [batch, height, width, channels].
        if images.ndim != 4:
            raise ValueError(f"LoopLastFrame expected IMAGE [B,H,W,C], got {tuple(images.shape)}")
        return (images[-1:].contiguous(),)

NODE_CLASS_MAPPINGS = {"LoopLastFrame": LoopLastFrame}
NODE_DISPLAY_NAME_MAPPINGS = {"LoopLastFrame": "Loop Last Frame"}
