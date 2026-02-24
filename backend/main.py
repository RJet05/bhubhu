from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import pandas as pd
import os
from typing import Optional

app = FastAPI(title="Carbon-Wise API")

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global variable to store dataset
df = None

class CarResult(BaseModel):
    make: str
    model: str
    year: int
    manufacturing_co2: float
    use_phase_co2: float
    total_lifecycle_co2: float

class ComparisonRequest(BaseModel):
    daily_mileage: float
    ownership_years: float
    vehicle_segment: str

class ComparisonResponse(BaseModel):
    lifetime_km: float
    overall_top_3: list[CarResult]
    petrol_diesel_top_3: list[CarResult]
    ev_top_3: list[CarResult]
    hybrid_top_3: list[CarResult]

def get_top_3(vehicles_df, lifetime_km):
    if vehicles_df.empty:
        return []

    # Keep latest model per make+model
    vehicles_df = vehicles_df.sort_values(
        by=["make", "model", "year"],
        ascending=[True, True, False]
    ).drop_duplicates(subset=["make", "model"], keep="first")

    results = []

    for _, vehicle in vehicles_df.iterrows():
        kg_per_km = convert_gpm_to_kg_per_km(vehicle['co2TailpipeGpm'])
        use_phase_co2 = kg_per_km * lifetime_km
        manufacturing_co2 = vehicle['total_manufacturing_co2_kg']
        total_lifecycle_co2 = manufacturing_co2 + use_phase_co2

        results.append({
            "make": vehicle['make'],
            "model": vehicle['model'],
            "year": int(vehicle['year']),
            "manufacturing_co2": round(manufacturing_co2, 2),
            "use_phase_co2": round(use_phase_co2, 2),
            "total_lifecycle_co2": round(total_lifecycle_co2, 2),
            "_sort": total_lifecycle_co2
        })

    results.sort(key=lambda x: x["_sort"])
    top_3 = results[:3]

    for car in top_3:
        del car["_sort"]

    return [CarResult(**car) for car in top_3]

def classify_powertrain(row):
    fuel1 = str(row.get("fuelType1", "")).lower()
    fuel2 = str(row.get("fuelType2", "")).lower()
    atv = str(row.get("atvType", "")).lower()

    # --- EV ---
    if "electricity" in fuel1:
        return "EV"

    # --- Plug-in Hybrid ---
    if "plug-in" in atv:
        return "Plug-in Hybrid"

    # --- Hybrid (Gasoline + Electric) ---
    if "electricity" in fuel2:
        return "Hybrid"

    # --- Diesel ---
    if "diesel" in fuel1:
        return "Diesel"

    # --- Petrol ---
    if "gasoline" in fuel1:
        return "Petrol"

    return "Other"

def clean_segment(segment: str) -> str:
    segment_lower = segment.lower()

    # ---- SUV ----
    if "sport utility" in segment_lower:
        if "small" in segment_lower:
            return "SUV (Small)"
        else:
            return "SUV (Standard / Large)"

    # ---- Pickup ----
    elif "pickup" in segment_lower:
        if "small" in segment_lower:
            return "Pickup Truck (Small)"
        else:
            return "Pickup Truck (Standard)"

    # ---- Van ----
    elif "van" in segment_lower or "minivan" in segment_lower:
        if "cargo" in segment_lower:
            return "Van (Cargo)"
        else:
            return "Van (Passenger / Minivan)"

    # ---- Special Purpose ----
    elif "special purpose" in segment_lower:
        return "Special Purpose Vehicle"

    else:
        return "Other"


def load_dataset():
    global df

    csv_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "data",
        "vehicles_with_full_manufacturing_co2.csv"
    )

    if os.path.exists(csv_path):
        df = pd.read_csv(csv_path)

        # 🔥 Apply segment cleaning here
        df["Segment_Cleaned"] = df["VClass"].apply(clean_segment)

        df["Powertrain"] = df.apply(classify_powertrain, axis=1)

        print(f"✅ Dataset loaded: {len(df)} vehicles found")
        print("Available cleaned segments:")
        print(df["Segment_Cleaned"].unique())

    else:
        print(f"❌ CSV file not found at {csv_path}")
        df = pd.DataFrame()


def convert_gpm_to_kg_per_km(gpm):
    """
    Convert co2TailpipeGpm (grams per mile) to kg per km
    1 mile = 1.60934 km
    1 gram = 0.001 kg
    
    kg_per_km = (gpm / 1000) / 1.60934
    """
    if gpm == 0:
        return 0
    return (gpm / 1000) / 1.60934


@app.on_event("startup")
async def startup_event():
    """Load dataset when the app starts"""
    load_dataset()


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "dataset_loaded": len(df) > 0 if df is not None else False,
        "total_vehicles": len(df) if df is not None else 0
    }


@app.get("/segments")
async def get_vehicle_segments():
    """Get all available vehicle segments"""
    if df is None or df.empty:
        raise HTTPException(status_code=500, detail="Dataset not loaded")
    
    segments = sorted(
    df["Segment_Cleaned"]
    .loc[df["Segment_Cleaned"] != "Other"]
    .unique()
    .tolist())

    # Pre-sort by make, model, year descending
    df.sort_values(by=["make", "model", "year"], ascending=[True, True, False], inplace=True)

    return {"segments": segments}


@app.post("/compare", response_model=ComparisonResponse)
async def compare_vehicles(request: ComparisonRequest):
    """
    Compare vehicles based on lifecycle CO2 emissions
    
    Request body:
    {
        "daily_mileage": number,
        "ownership_years": number,
        "vehicle_segment": string
    }
    """
    
    if df is None or df.empty:
        raise HTTPException(status_code=500, detail="Dataset not loaded")
    
    # Validate input
    if request.daily_mileage <= 0:
        raise HTTPException(status_code=400, detail="Daily mileage must be greater than 0")
    
    if request.ownership_years <= 0:
        raise HTTPException(status_code=400, detail="Ownership years must be greater than 0")
    
    if not request.vehicle_segment:
        raise HTTPException(status_code=400, detail="Vehicle segment is required")
    
    # Calculate lifetime distance
    lifetime_km = request.daily_mileage * 365 * request.ownership_years
    
    # Filter vehicles by segment
    segment_vehicles = df[df["Segment_Cleaned"] == request.vehicle_segment].copy()

    section1_df = segment_vehicles[
    segment_vehicles["year"].isin([2023, 2024, 2025, 2026])]

    #Section 1 - 
    overall_top_3 = get_top_3(section1_df, lifetime_km)

    #Section 2 - Petrol + Diesel, All Years
    petrol_diesel_df = segment_vehicles[
    segment_vehicles["Powertrain"].isin(["Petrol", "Diesel"])]

    petrol_diesel_top_3 = get_top_3(petrol_diesel_df, lifetime_km)

    #Section 3 - EV, All Years
    ev_df = segment_vehicles[
    segment_vehicles["Powertrain"] == "EV"]
    ev_top_3 = get_top_3(ev_df, lifetime_km)

    #Section 4 - Hybrid, All Years
    hybrid_df = segment_vehicles[
    segment_vehicles["Powertrain"] == "Hybrid"]
    hybrid_top_3 = get_top_3(hybrid_df, lifetime_km)
    
    
    return ComparisonResponse(
    lifetime_km=round(lifetime_km, 2),
    overall_top_3=overall_top_3,
    petrol_diesel_top_3=petrol_diesel_top_3,
    ev_top_3=ev_top_3,
    hybrid_top_3=hybrid_top_3
)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
