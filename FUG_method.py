import numpy as np


class FUG_Estimator:
    def __init__(self, alpha: float, xF: float, purity_target: float, recovery_target: float = None):
        """
        初始化 FUG 求解器
        :param alpha: 相对挥发度 (对于萃取精馏，需传入溶剂诱导的伪相对挥发度)
        :param xF: 进料中轻组分的摩尔分数
        :param purity_target: 塔顶轻组分纯度目标 (如 0.99)
        :param recovery_target: 轻组分回收率 (若为 None，则假设回收率等于纯度目标)
        """
        self.alpha = float(alpha)
        self.xF = float(xF)
        self.xD = float(purity_target)
        # 默认回收率与纯度一致
        self.rec = float(recovery_target) if recovery_target else self.xD

    def calculate_bounds(self) -> dict:
        """执行 FUG 联立求解，输出 Aspen RadFrac 搜索空间"""
        if self.alpha <= 1.01:
            return {"error": "相对挥发度极度接近或低于1，常规 FUG 算法发散，需引入特殊分离或校正 alpha。"}

        try:
            # 1. 质量守恒推导塔底组成 (xB)
            # D * xD = F * xF * rec
            # F = 1 (基准), D = xF * rec / xD, B = 1 - D
            D = (self.xF * self.rec) / self.xD
            B = 1.0 - D

            # 塔底轻组分流率 = 总进料轻组分 - 塔顶轻组分
            bottom_lk_flow = self.xF * (1 - self.rec)
            xB = bottom_lk_flow / B if B > 0 else 0.001

            # 防止对数发散的极值保护
            xD_safe = min(self.xD, 0.9999)
            xB_safe = max(xB, 0.0001)

            # 2. Fenske: 计算 Nmin
            term1 = xD_safe / (1 - xD_safe)
            term2 = (1 - xB_safe) / xB_safe
            n_min = np.log(term1 * term2) / np.log(self.alpha)

            # 3. Underwood: 计算 Rmin (假设进料 q=1 泡点进料)
            # Rmin = (1 / (alpha - 1)) * (xD/xF - alpha * (1-xD)/(1-xF))
            r_min = (1 / (self.alpha - 1)) * ((xD_safe / self.xF) - self.alpha * ((1 - xD_safe) / (1 - self.xF)))
            r_min = max(r_min, 0.1)  # 物理界限约束

            # 4. Kirkbride: 进料板预估
            # log(Nm/Np) = 0.206 * log[ (B/D) * (x_F,HK / x_F,LK) * (x_B,LK / x_D,HK)^2 ]
            kirk_term = (B / D) * ((1 - self.xF) / self.xF) * ((xB_safe / (1 - xD_safe)) ** 2)
            ratio_Nm_Np = 10 ** (0.206 * np.log10(kirk_term))

            # 5. 工程边界生成 (用于后续优化)
            anchor_R = round(r_min * 1.5, 2)
            anchor_N = int(round(n_min * 2.0))

            # 精馏段板数 Nm = N_total * [ratio / (1 + ratio)]
            feed_stage_ideal = int(round(anchor_N * (ratio_Nm_Np / (1 + ratio_Nm_Np))))

            return {
                "success": True,
                "theoretical_limits": {
                    "N_min": round(n_min, 2),
                    "R_min": round(r_min, 2)
                },
                "aspen_radfrac_anchors": {
                    "NSTAGE_suggested": anchor_N,
                    "FEED_STAGE_suggested": feed_stage_ideal,
                    "REFLUX_RATIO_suggested": anchor_R
                },
                "optimization_search_space": {
                    "NSTAGE_bounds": [max(5, int(n_min)), int(n_min * 5)],
                    "FEED_STAGE_bounds": [max(2, feed_stage_ideal - int(anchor_N * 0.2)),
                                          min(anchor_N - 1, feed_stage_ideal + int(anchor_N * 0.2))],
                    "REFLUX_RATIO_bounds": [round(r_min * 1.05, 2), round(r_min * 5.0, 2)]
                }
            }
        except Exception as e:
            return {"success": False, "error": f"FUG 计算异常: {str(e)}"}

# === 测试调用 ===
# 假设大模型判定为常规分离，给出的理想 alpha = 2.1，进料含轻组分 50%，纯度要求 99%
# fug = FUG_Estimator(alpha=2.1, xF=0.5, purity_target=0.99)
# print(fug.calculate_bounds())